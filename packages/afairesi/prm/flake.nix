{ inputs }:
let
  builders = import ./builders.nix;
  lib = builders // {
    mkFormatter =
      { self }:
      inputs.nixpkgs.lib.genAttrs (builtins.attrNames self.packages) (
        system:
        import ../../../formatter.nix {
          inherit inputs self;
          flake = self.outPath;
          pkgs = inputs.nixpkgs.legacyPackages.${system};
        }
      );
    blueprint =
      arguments:
      let
        repositoryInputs = arguments.inputs;
        base = inputs.blueprint arguments;
        nixlib = inputs.nixpkgs.lib;
        source = repositoryInputs.self.outPath;
        packagePath = source + "/packages";
        names = builtins.attrNames (
          nixlib.filterAttrs (
            name: type: type == "directory" && builtins.pathExists (packagePath + "/${name}/main.py")
          ) (if builtins.pathExists packagePath then builtins.readDir packagePath else { })
        );
        systems = builtins.attrNames base.packages;
        environments = nixlib.genAttrs systems (
          system:
          nixlib.genAttrs names (
            name:
            builders.mkTestEnvironment {
              pkgs = (repositoryInputs.nixpkgs or inputs.nixpkgs).legacyPackages.${system};
              packageDrv = repositoryInputs.self.packages.${system}.${name};
            }
          )
        );
      in
      (builtins.removeAttrs base [ "__functor" ])
      // {
        apps = nixlib.genAttrs systems (
          system:
          let
            pkgs = (repositoryInputs.nixpkgs or inputs.nixpkgs).legacyPackages.${system};
            packages = nixlib.filterAttrs (
              _: package: package ? runtimeHook && package.runtimeHook != ""
            ) repositoryInputs.self.packages.${system};
            generated = nixlib.mapAttrs (name: package: {
              inherit (package) meta;
              type = "app";
              program = toString (pkgs.writeShellScript "${name}-run" ''
                set -e
                ${package.runtimeHook}
                exec ${nixlib.getExe package} "$@"
              '');
            }) packages;
          in
          assert nixlib.assertMsg (
            builtins.intersectAttrs (base.apps.${system} or { }) generated == { }
          ) "Afairesi generated runtime apps collide with existing app names";
          (base.apps.${system} or { }) // generated
        );
        legacyPackages = nixlib.genAttrs systems (
          system:
          let
            generated = nixlib.listToAttrs (
              nixlib.concatMap (
                name:
                [
                  {
                    name = "${name}-test-environment";
                    value = environments.${system}.${name}.manifest;
                  }
                ]
                ++ nixlib.optional (builtins.pathExists (source + "/checks/${name}/default.nix")) {
                  name = "${name}-coverage";
                  value = builders.mkCoverage {
                    packageName = name;
                    packageDrv = repositoryInputs.self.packages.${system}.${name};
                    check = repositoryInputs.self.checks.${system}.${name};
                  };
                }
              ) names
            );
          in
          assert nixlib.assertMsg (
            builtins.intersectAttrs (base.legacyPackages.${system} or { }) generated == { }
          ) "Afairesi generated test outputs collide with package names";
          (base.legacyPackages.${system} or { }) // generated
        );
        devShells = nixlib.genAttrs systems (
          system:
          (base.devShells.${system} or { })
          // nixlib.mapAttrs' (name: environment: {
            name = "${name}-test";
            value = environment.shell;
          }) environments.${system}
        );
      };
  };
in
lib

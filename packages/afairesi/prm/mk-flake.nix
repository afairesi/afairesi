{ inputs }:
let
  afairesiInputs = inputs;
  builders = import ./builders.nix;
in
# Assemble canonical packages/*/default.nix and hosts/*/configuration.nix.
# Packages receive inputs (with a fallback nixpkgs), pkgs, system, and pname;
# hosts receive inputs and hostName. One configured package set per system
# supplies packages, test environments, checks, and runtime app wrappers.
{
  inputs,
  systems ? [
    "aarch64-darwin"
    "aarch64-linux"
    "x86_64-linux"
  ],
  nixpkgs ? { },
}:
let
  repositoryInputs = {
    inherit (afairesiInputs) nixpkgs;
  }
  // inputs;
  lib = repositoryInputs.nixpkgs.lib;
  source = repositoryInputs.self.outPath;
  packagePath = source + "/packages";
  discover =
    path: marker:
    builtins.attrNames (
      lib.filterAttrs (
        name: type: type == "directory" && builtins.pathExists (path + "/${name}/${marker}")
      ) (if builtins.pathExists path then builtins.readDir path else { })
    );
  packageNames = discover packagePath "default.nix";
  pythonNames = builtins.filter (
    name: builtins.pathExists (packagePath + "/${name}/main.py")
  ) packageNames;
  moduleNames = map (name: builtins.replaceStrings [ "-" ] [ "_" ] name) pythonNames;
  packageSets = lib.genAttrs systems (
    system:
    if (nixpkgs.config or { }) == { } && (nixpkgs.overlays or [ ]) == [ ] then
      repositoryInputs.nixpkgs.legacyPackages.${system}
    else
      import repositoryInputs.nixpkgs {
        inherit system;
        config = nixpkgs.config or { };
        overlays = nixpkgs.overlays or [ ];
      }
  );
  supported =
    system:
    lib.filterAttrs (
      _: package:
      (package.meta.platforms or [ ]) == [ ]
      || lib.meta.availableOn packageSets.${system}.stdenv.hostPlatform package
    );
  namesFor =
    system:
    builtins.filter (name: builtins.hasAttr name repositoryInputs.self.packages.${system}) pythonNames;
  testedNames =
    system:
    builtins.filter (name: builtins.pathExists (packagePath + "/${name}/test_main.py")) (
      namesFor system
    );
  hostsFor =
    system:
    lib.filterAttrs (_: configuration: configuration.pkgs.stdenv.hostPlatform.system == system) (
      repositoryInputs.self.nixosConfigurations or { }
    );
  mergeChecks =
    groups:
    builtins.foldl' (
      result: group:
      assert lib.assertMsg (
        builtins.intersectAttrs result group == { }
      ) "Afairesi generated check names collide";
      result // group
    ) { } groups;
  environments = lib.genAttrs systems (
    system:
    lib.genAttrs (namesFor system) (
      name:
      builders.mkTestEnvironment {
        pkgs = packageSets.${system};
        packageDrv = repositoryInputs.self.packages.${system}.${name};
      }
    )
  );
in
{
  packages = lib.genAttrs systems (
    system:
    assert lib.assertMsg (
      builtins.length (lib.unique moduleNames) == builtins.length moduleNames
    ) "Afairesi Python packages have colliding installed module names";
    supported system (
      lib.genAttrs packageNames (
        pname:
        lib.callPackageWith {
          inputs = repositoryInputs;
          pkgs = packageSets.${system};
          inherit system pname;
        } (packagePath + "/${pname}/default.nix") { }
      )
    )
  );
  nixosConfigurations = lib.genAttrs (discover (source + "/hosts") "configuration.nix") (
    hostName:
    repositoryInputs.nixpkgs.lib.nixosSystem {
      specialArgs = {
        inputs = repositoryInputs;
        inherit hostName;
      };
      modules = [
        (source + "/hosts/${hostName}/configuration.nix")
        (
          { config, lib, ... }:
          lib.optionalAttrs ((nixpkgs.config or { }) != { } || (nixpkgs.overlays or [ ]) != [ ]) {
            nixpkgs.pkgs = lib.mkDefault packageSets.${config.nixpkgs.hostPlatform.system};
          }
        )
      ];
    }
  );
  checks = lib.genAttrs systems (
    system:
    mergeChecks [
      (lib.genAttrs (testedNames system) (
        name:
        import ./python-check.nix {
          pkgs = packageSets.${system};
          packageDrv = repositoryInputs.self.packages.${system}.${name};
          packageName = name;
          testEnvironment = environments.${system}.${name};
        }
      ))
      (lib.mapAttrs' (
        host: _:
        lib.nameValuePair "${host}VmWithDisko" (
          import ./host-check.nix {
            inputs = repositoryInputs;
            pkgs = packageSets.${system};
            inherit host;
          }
        )
      ) (hostsFor system))
      (lib.mapAttrs' (
        name: configuration: lib.nameValuePair "nixos-${name}" configuration.config.system.build.toplevel
      ) (hostsFor system))
      (lib.mapAttrs' (
        name: shell: lib.nameValuePair "devshell-${name}" shell
      ) repositoryInputs.self.devShells.${system})
      (mergeChecks (
        lib.mapAttrsToList (
          name: package:
          {
            "pkgs-${name}" = package;
          }
          // lib.mapAttrs' (testName: test: lib.nameValuePair "pkgs-${name}-${testName}" test) (
            supported system (package.passthru.tests or { })
          )
        ) repositoryInputs.self.packages.${system}
      ))
      (lib.optionalAttrs (repositoryInputs.self ? formatter) {
        formatter =
          repositoryInputs.self.formatter.${system}.passthru.tests.check
            or repositoryInputs.self.formatter.${system};
      })
    ]
  );
  apps = lib.genAttrs systems (
    system:
    lib.mapAttrs
      (name: package: {
        meta = {
          description = "Run ${name}";
        }
        // (package.meta or { });
        type = "app";
        program = toString (
          packageSets.${system}.writeShellScript "${name}-run" ''
            set -e
            ${package.runtimeHook}
            exec ${lib.getExe package} "$@"
          ''
        );
      })
      (
        lib.filterAttrs (
          _: package: package ? runtimeHook && package.runtimeHook != ""
        ) repositoryInputs.self.packages.${system}
      )
  );
  legacyPackages = lib.genAttrs systems (
    system:
    lib.listToAttrs (
      lib.concatMap (
        name:
        [
          {
            name = "${name}-test-environment";
            value = environments.${system}.${name}.manifest;
          }
        ]
        ++ lib.optional (builtins.elem name (testedNames system)) {
          name = "${name}-coverage";
          value = builders.mkCoverage {
            packageDrv = repositoryInputs.self.packages.${system}.${name};
            check = repositoryInputs.self.checks.${system}.${name};
          };
        }
      ) (namesFor system)
    )
  );
  devShells = lib.genAttrs systems (
    system:
    lib.mapAttrs' (name: environment: {
      name = "${name}-test";
      value = environment.shell;
    }) environments.${system}
  );
}

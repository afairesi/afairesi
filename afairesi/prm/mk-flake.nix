{ inputs }:
let
  afairesiInputs = inputs;
  builders = import ./builders.nix;
in
# Assemble canonical */default.nix and */configuration.nix.
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
  inherit (repositoryInputs) self;
  lib = repositoryInputs.nixpkgs.lib;
  source = self.outPath;
  discover =
    marker:
    builtins.attrNames (
      lib.filterAttrs (
        name: type:
        type == "directory"
        && !(builtins.elem name [
          "prm"
          "tmp"
        ])
        && builtins.pathExists (source + "/${name}/${marker}")
      ) (if builtins.pathExists source then builtins.readDir source else { })
    );
  packageNames = discover "default.nix";
  pythonNames = builtins.filter (
    name: builtins.pathExists (source + "/${name}/main.py")
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
    system: builtins.filter (name: builtins.hasAttr name self.packages.${system}) pythonNames;
  testedNames =
    system:
    builtins.filter (name: builtins.pathExists (source + "/${name}/test_main.py")) (namesFor system);
  hostsFor =
    system:
    lib.filterAttrs (_: configuration: configuration.pkgs.stdenv.hostPlatform.system == system) (
      self.nixosConfigurations or { }
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
        packageDrv = self.packages.${system}.${name};
      }
    )
  );
in
{
  formatter = lib.genAttrs systems (
    system:
    import ./formatter.nix {
      inputs = afairesiInputs;
      inherit self;
      flake = source;
      pkgs = packageSets.${system};
    }
  );
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
        } (source + "/${pname}/default.nix") { }
      )
    )
  );
  nixosConfigurations = lib.genAttrs (discover "configuration.nix") (
    hostName:
    repositoryInputs.nixpkgs.lib.nixosSystem {
      specialArgs = {
        inputs = repositoryInputs;
        inherit hostName;
      };
      modules = [
        (source + "/${hostName}/configuration.nix")
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
    mergeChecks (
      [
        (lib.genAttrs (testedNames system) (
          name:
          import ./python-check.nix {
            pkgs = packageSets.${system};
            packageDrv = self.packages.${system}.${name};
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
        (lib.mapAttrs' (name: shell: lib.nameValuePair "devshell-${name}" shell) self.devShells.${system})
        {
          formatter = self.formatter.${system}.passthru.tests.check or self.formatter.${system};
        }
      ]
      ++ lib.mapAttrsToList (
        name: package:
        {
          "pkgs-${name}" = package;
        }
        // lib.mapAttrs' (testName: test: lib.nameValuePair "pkgs-${name}-${testName}" test) (
          supported system (package.passthru.tests or { })
        )
      ) self.packages.${system}
    )
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
        ) self.packages.${system}
      )
  );
  legacyPackages = lib.genAttrs systems (
    system:
    lib.mapAttrs' (
      name: environment: lib.nameValuePair "${name}-test-environment" environment.manifest
    ) environments.${system}
    // lib.genAttrs' (testedNames system) (
      name:
      lib.nameValuePair "${name}-coverage" (
        builders.mkCoverage {
          packageDrv = self.packages.${system}.${name};
          check = self.checks.${system}.${name};
        }
      )
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

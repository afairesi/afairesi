let
  lib = rec {
    mkPythonPackage = import ./python-package.nix;
    mkPythonCheck =
      arguments@{ pkgs, packageDrv, ... }:
      import ./python-check.nix (
        arguments
        // {
          testEnvironment = mkTestEnvironment { inherit pkgs packageDrv; };
        }
      );
    mkHostCheck = import ./host-check.nix;
    mkCoverage = import ./coverage.nix;
    mkTestEnvironment =
      { pkgs, packageDrv }:
      let
        dependencies = pkgs.lib.concatMap (name: packageDrv.${name} or [ ]) [
          "buildInputs"
          "checkInputs"
          "nativeBuildInputs"
          "nativeCheckInputs"
          "propagatedBuildInputs"
          "propagatedNativeBuildInputs"
        ];
        python = packageDrv.python.withPackages (
          ps:
          dependencies
          ++ [
            ps.hypothesis
            ps.pytest
          ]
        );
      in
      {
        inherit dependencies python;
        manifest = pkgs.writeText "test-environment.json" (
          builtins.toJSON {
            python = "${python}/bin/python";
            path = pkgs.lib.makeBinPath dependencies;
          }
        );
        shell = pkgs.mkShell {
          inputsFrom = [ packageDrv ];
          packages = dependencies ++ [ python ];
        };
      };
  };
in
lib

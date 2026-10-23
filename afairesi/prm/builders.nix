{
  mkLean4Package = import ./lean4-package.nix;
  mkPythonPackage = import ./python-package.nix;
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
    if packageDrv ? lean4 then
      {
        inherit dependencies;
        inherit (packageDrv) lean4;
        shell = pkgs.mkShell {
          inputsFrom = [ packageDrv ];
          packages = dependencies ++ [ packageDrv.lean4 ];
          buildInputs = [ packageDrv ];
        };
      }
    else
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
}

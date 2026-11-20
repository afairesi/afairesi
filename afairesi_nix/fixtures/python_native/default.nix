{
  inputs,
  pkgs,
  pname,
  system,
}:
pkgs.python3Packages.buildPythonPackage {
  inherit pname;
  build-system = [ pkgs.python3Packages.setuptools ];
  meta.description = "A standard Python package with its own source layout";
  passthru = {
    example = inputs.self.packages.${system}.example;
    tests.contract =
      pkgs.runCommand "python_native-contract"
        {
          nativeBuildInputs = [
            (pkgs.python3.withPackages (_: [ inputs.self.packages.${system}.python_native ]))
          ];
        }
        ''
          python -c 'from native_example import answer; assert answer() == 42'
          touch "$out"
        '';
  };
  pyproject = true;
  pythonImportsCheck = [ "native_example" ];
  src = ./.;
  version = "0.0.0";
}

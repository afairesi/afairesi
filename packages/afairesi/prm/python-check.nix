{
  pkgs,
  packageDrv,
  packageName,
  testEnvironment,
}:
let
  pythonEnv = testEnvironment.python;
in
pkgs.runCommand packageName
  (
    {
      inherit (packageDrv) src;
      nativeBuildInputs = testEnvironment.dependencies ++ [ pythonEnv ];
    }
    // pkgs.lib.optionalAttrs (packageDrv.meta ? mainProgram) {
      PACKAGE_E2E_EXECUTABLE = pkgs.lib.getExe packageDrv;
    }
  )
  ''
    export HOME="$(mktemp -d)"
    mkdir -p "$out" packages
    ln -s "$src" "packages/${packageName}"
    export PYTHONPATH="$PWD:$PYTHONPATH"
    cd "$out"
    "${pythonEnv}/bin/python" -m pytest -p no:cacheprovider --import-mode=importlib \
      "$src/test_main.py" "''${afairesi_pytest_args[@]}"
  ''

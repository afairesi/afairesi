{
  pkgs,
  packageDrv,
  packageName,
  testEnvironment,
}:
let
  pythonEnv = testEnvironment.python;
  source = builtins.readFile ../main.py;
  testSupportSource =
    "from __future__ import annotations\n\ndef make_test_report_plugin"
    + builtins.head (
      builtins.split "\ndef python_test_support_source" (
        builtins.elemAt (builtins.split "\ndef make_test_report_plugin" source) 2
      )
    );
  bootstrap = builtins.toFile "python-check.py" (testSupportSource + "\nrun_python_check()\n");
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
    export src PACKAGE_E2E_EXECUTABLE
    export HOME="$(mktemp -d)"
    mkdir -p "$out" packages
    ln -s "$src" "packages/${packageName}"
    export PYTHONPATH="$PWD:$PYTHONPATH"
    cd "$out"
    "${pythonEnv}/bin/python" "${bootstrap}" "''${afairesi_pytest_args[@]}"
  ''

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
    export src PACKAGE_E2E_EXECUTABLE
    export HOME="$(mktemp -d)"
    mkdir -p "$out" packages
    ln -s "$src" "packages/${packageName}"
    export PYTHONPATH="$PWD:$PYTHONPATH"
    cd "$out"
    "${pythonEnv}/bin/python" - <<'PYTHON'
    import os
    import sys
    from hypothesis import Phase, settings
    settings.register_profile("explicit", phases=[Phase.explicit])
    settings.load_profile("explicit")
    import pytest
    sys.exit(pytest.main([
        "-p", "no:cacheprovider",
        "--import-mode=importlib",
        os.environ["src"] + "/test_main.py",
    ]))
    PYTHON
  ''

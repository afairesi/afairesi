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
    "${pythonEnv}/bin/python" -c '
    import sys
    import pytest
    class Defaults:
        @pytest.hookimpl(tryfirst=True)
        def pytest_load_initial_conftests(self, early_config, parser, args):
            from hypothesis import settings
            settings.register_profile("afairesi", max_examples=10)
            settings.load_profile("afairesi")
    sys.exit(pytest.main(plugins=[Defaults()]))
    ' -p no:cacheprovider --import-mode=importlib \
      "$src/test_main.py" "''${afairesi_pytest_args[@]}"
  ''

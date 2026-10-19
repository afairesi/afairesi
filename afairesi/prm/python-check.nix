{
  pkgs,
  packageDrv,
  packageName,
  testEnvironment,
}:
pkgs.runCommand packageName
  (
    {
      inherit (packageDrv) src;
      nativeBuildInputs = testEnvironment.dependencies ++ [ testEnvironment.python ];
    }
    // pkgs.lib.optionalAttrs (packageDrv.meta ? mainProgram) {
      PACKAGE_E2E_EXECUTABLE = pkgs.lib.getExe packageDrv;
    }
  )
  ''
    export HOME="$(mktemp -d)"
    mkdir -p "$out"
    ln -s "$src" "${packageName}"
    export PYTHONPATH="$PWD:$PYTHONPATH"
    cd "$out"
    "${testEnvironment.python}/bin/python" -c '
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

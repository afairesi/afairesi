{
  pkgs,
  packageDrv,
  packageName,
  testEnvironment,
}:
pkgs.runCommand packageName
  (
    {
      nativeBuildInputs = testEnvironment.dependencies ++ [ testEnvironment.lean4 ];
      buildInputs = [ packageDrv ];
    }
    // pkgs.lib.optionalAttrs (packageDrv.meta ? mainProgram) {
      PACKAGE_E2E_EXECUTABLE = pkgs.lib.getExe packageDrv;
    }
  )
  ''
    cp -R ${packageDrv} source
    chmod -R u+w source
    cd source
    lake lean test_main.lean
    mkdir -p "$out"
  ''

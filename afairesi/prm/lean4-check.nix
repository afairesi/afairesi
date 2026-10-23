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
      nativeBuildInputs = testEnvironment.dependencies ++ [ testEnvironment.lean4 ];
      buildInputs = [ packageDrv ];
    }
    // pkgs.lib.optionalAttrs (packageDrv.meta ? mainProgram) {
      PACKAGE_E2E_EXECUTABLE = pkgs.lib.getExe packageDrv;
    }
  )
  ''
    cp -R "$src" source
    chmod -R u+w source
    cd source
    lean -DwarningAsError=true test_main.lean
    mkdir -p "$out"
  ''

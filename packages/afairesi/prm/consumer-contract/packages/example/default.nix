{ inputs, pkgs, ... }:
inputs.afairesi.lib.mkPythonPackage {
  inherit pkgs;
  executable = true;
  meta.description = "Afairesi consumer contract example";
  runtimeHook = ''
    if [ "''${AFAIRESI_CONSUMER_FAIL:-}" = 1 ]; then
      exit 23
    fi
    export AFAIRESI_CONSUMER_HOOK=loaded
  '';
  passthru.consumerConfig = {
    inherit (pkgs.config) allowUnfree cudaSupport;
  };
  src = ./.;
}

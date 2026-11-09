{ inputs, pkgs, ... }:
inputs.afairesi.lib.mkPythonPackage {
  inherit pkgs;
  executable = true;
  meta.description = "Afairesi consumer contract example";
  passthru.consumerConfig = {
    inherit (pkgs) afairesiContract;
    inherit (pkgs.config) allowUnfree cudaSupport;
  };
  runtimeHook = ''
    if [ "''${AFAIRESI_CONSUMER_FAIL:-}" = 1 ]; then
      exit 23
    fi
    export AFAIRESI_CONSUMER_HOOK=loaded
  '';
  src = ./.;
}

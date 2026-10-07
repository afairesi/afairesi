{ inputs, pkgs, ... }:
inputs.afairesi.lib.mkPythonPackage {
  inherit pkgs;
  executable = true;
  passthru.consumerConfig = {
    inherit (pkgs.config) allowUnfree cudaSupport;
  };
  src = ./.;
}

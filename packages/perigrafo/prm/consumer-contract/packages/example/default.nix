{ inputs, pkgs, ... }:
inputs.perigrafo.lib.mkPythonPackage {
  inherit pkgs;
  executable = true;
  passthru.consumerConfig = {
    inherit (pkgs.config) allowUnfree cudaSupport;
  };
  src = ./.;
}

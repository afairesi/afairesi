{ inputs, pkgs, ... }:
inputs.afairesi.lib.mkBashPackage {
  inherit pkgs;
  runtimeEnv.GREETING = "two words";
  runtimeInputs = [ pkgs.hello ];
  src = ./.;
}

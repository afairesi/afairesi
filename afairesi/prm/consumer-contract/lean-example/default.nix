{
  inputs,
  pkgs,
  system,
  ...
}:
inputs.afairesi.lib.mkLean4Package {
  inherit pkgs;
  executable = true;
  leanDeps = [ inputs.self.packages.${system}.test_main ];
  meta.description = "Lean executable consumer contract";
  passthru.checkInputs = [ pkgs.hello ];
  preBuild = ''
    substituteInPlace prm/Support.lean --replace-fail '"before-build"' '"resource"'
  '';
  runtimeHook = ''
    export AFAIRESI_LEAN4_HOOK=loaded
  '';
  src = ./.;
}

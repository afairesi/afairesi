{
  inputs,
  pkgs,
  system,
  ...
}:
inputs.afairesi.lib.mkLean4Package {
  inherit pkgs;
  executable = false;
  meta.description = "Lean library consumer contract";
  passthru.checkInputs = [ inputs.self.packages.${system}.lean-example ];
  postPatch = "test -f main.lean";
  src = ./.;
}

arguments@{ pkgs, src, ... }:
pkgs.stdenv.mkDerivation (
  {
    nativeBuildInputs = [ pkgs.texliveFull ];
    version = "0.0.0";
  }
  // builtins.removeAttrs arguments [ "pkgs" ]
  // {
    pname = baseNameOf src;
    inherit src;
    buildPhase = ''
      latexmk -pdf ms.tex
    '';
    installPhase = ''
      install -Dm644 ms.pdf "$out/ms.pdf"
    '';
    strictDeps = true;
  }
)

{ inputs, pkgs, ... }:
(inputs.afairesi or inputs.self).lib.mkHtmlPackage {
  inherit pkgs;
  meta.description = "Afairesi: a computing environment designed to minimize independent decisions.";
  postInstall = ''
    cp ${pkgs.writeText "afairesi-demo.js" ''
      globalThis.afairesiDemoRecording = ${builtins.toJSON (builtins.readFile ./afairesi-demo.cast)};
    ''} "$out/afairesi-demo.js"
  '';
  src = ./.;
}

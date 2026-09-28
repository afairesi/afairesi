{ inputs, pkgs, ... }:
let
  pname = baseNameOf ./.;
  python = pkgs.python3;
  runtimeInputs = [
    inputs.self.packages.${pkgs.stdenv.system}.canonical_agent
    pkgs.asciinema
    pkgs.asciinema-agg
    pkgs.dejavu_fonts
    pkgs.ffmpeg
  ];
in
python.pkgs.buildPythonPackage {
  inherit pname;
  installPhase = ''
    install -Dm644 main.py "$out/${python.sitePackages}/$pname/__init__.py"
    mkdir -p "$out/bin"
    printf '%s\n' '#!${python.interpreter}' "from $pname import main" 'main()' > "$out/bin/$pname"
    chmod 755 "$out/bin/$pname"
    if [ -d prm ]; then
      cp -R prm/ "$out/${python.sitePackages}/$pname/"
    fi
  '';
  meta = {
    description = "Generate an MP4 demo of canonical_agent usage";
    mainProgram = pname;
  };
  nativeBuildInputs = [ pkgs.makeWrapper ];
  passthru.python = python;
  postFixup = ''
    wrapProgram "$out/bin/${pname}" \
      --prefix PATH : "${pkgs.lib.makeBinPath runtimeInputs}" \
      --set CANONICAL_AGENT_VIDEO_FONT_DIR "${pkgs.dejavu_fonts}/share/fonts/truetype"
  '';
  propagatedBuildInputs = runtimeInputs;
  pyproject = false;
  src = ./.;
  strictDeps = true;
  version = "0.0.0";
}

{ inputs, pkgs, ... }:
let
  pname = baseNameOf ./.;
  python = pkgs.python3;
  runtimeInputs = [
    inputs.self.packages.${pkgs.stdenv.system}.perigrafo_browser
    pkgs.chromium
    pkgs.ffmpeg
    pkgs.git
  ];
  videoTools = pkgs.runCommand "playwright-video-tools" { } ''
    mkdir -p "$out/ffmpeg-${pkgs.playwright-driver.browsersJSON.ffmpeg.revision}"
    ln -s ${pkgs.ffmpeg}/bin/ffmpeg "$out/ffmpeg-${pkgs.playwright-driver.browsersJSON.ffmpeg.revision}/ffmpeg-${
      if pkgs.stdenv.hostPlatform.isDarwin then "mac" else "linux"
    }"
  '';
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
    description = "Generate an MP4 demo of perigrafo_browser usage";
    mainProgram = pname;
  };
  nativeBuildInputs = [ pkgs.makeWrapper ];
  passthru.python = python;
  postFixup = ''
    wrapProgram "$out/bin/${pname}" \
      --prefix PATH : "${pkgs.lib.makeBinPath runtimeInputs}" \
      --set PLAYWRIGHT_BROWSERS_PATH "${videoTools}" \
      --set PERIGRAFO_BROWSER_VIDEO_CHROMIUM "${pkgs.chromium}/bin/chromium"
  '';
  propagatedBuildInputs = runtimeInputs ++ [ python.pkgs.playwright ];
  pyproject = false;
  src = ./.;
  strictDeps = true;
  version = "0.0.0";
}

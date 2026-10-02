{ inputs, pkgs, ... }:
let
  elk = pkgs.stdenvNoCC.mkDerivation {
    installPhase = ''
      install -Dm644 lib/elk.bundled.js "$out/elk.js"
      install -Dm644 LICENSE.md "$out/share/licenses/elkjs/LICENSE.md"
      mkdir -p "$out/nix-support"
      printf 'export CANONICAL_BROWSER_ELK=%s/elk.js\n' "$out" > "$out/nix-support/setup-hook"
    '';
    pname = "elkjs-browser";
    src = pkgs.fetchurl {
      hash = "sha256-wddxlyPgILEHJOPMvJNWlqKITx5402HN0DdmMJ6Ojio=";
      url = "https://registry.npmjs.org/elkjs/-/elkjs-0.12.0.tgz";
    };
    version = "0.12.0";
  };
  pname = baseNameOf ./.;
  python = pkgs.python3;
  runtimeInputs = [
    inputs.self.packages.${pkgs.stdenv.system}.git-canonical
    pkgs.git
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
    description = "Terminal and visual browser for Canonical packages, interfaces, tests, and changes";
    mainProgram = pname;
  };
  nativeBuildInputs = [
    elk
    pkgs.makeWrapper
  ];
  passthru = {
    elk = elk;
    python = python;
  };
  postFixup = ''
    cp ${elk}/elk.js "$out/${python.sitePackages}/$pname/prm/elk.js"
    mkdir -p "$out/share/licenses"
    cp -R ${elk}/share/licenses/elkjs "$out/share/licenses/"
    wrapProgram "$out/bin/${pname}" --prefix PATH : "${pkgs.lib.makeBinPath runtimeInputs}"
  '';
  propagatedBuildInputs = runtimeInputs;
  pyproject = false;
  src = ./.;
  strictDeps = true;
  version = "0.0.0";
}

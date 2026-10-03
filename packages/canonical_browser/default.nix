{ inputs, pkgs, ... }:
let
  g6 = pkgs.stdenvNoCC.mkDerivation {
    installPhase = ''
      install -Dm644 dist/g6.min.js "$out/g6.js"
      install -Dm644 LICENSE "$out/share/licenses/g6/LICENSE"
      mkdir -p "$out/nix-support"
      printf 'export CANONICAL_BROWSER_G6=%s/g6.js\n' "$out" > "$out/nix-support/setup-hook"
    '';
    pname = "g6-browser";
    src = pkgs.fetchurl {
      hash = "sha256-CjGj0LKAJmCuskBlpHuqoKFnK0HokyR8gZwzr+ihRa4=";
      url = "https://registry.npmjs.org/@antv/g6/-/g6-5.1.1.tgz";
    };
    version = "5.1.1";
  };
  pname = baseNameOf ./.;
  python = pkgs.python3;
  runtimeInputs = [
    inputs.self.packages.${pkgs.stdenv.system}.git-canonical
    pkgs.diffoscope
    pkgs.git
    pkgs.nix
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
    description = "Web diagram browser for Canonical directories, packages, hosts, and changes";
    mainProgram = pname;
  };
  nativeBuildInputs = [
    g6
    pkgs.makeWrapper
  ];
  passthru = {
    g6 = g6;
    python = python;
  };
  postFixup = ''
    cp ${g6}/g6.js "$out/${python.sitePackages}/$pname/prm/g6.js"
    mkdir -p "$out/share/licenses"
    cp -R ${g6}/share/licenses/g6 "$out/share/licenses/"
    wrapProgram "$out/bin/${pname}" --prefix PATH : "${pkgs.lib.makeBinPath runtimeInputs}"
  '';
  propagatedBuildInputs = runtimeInputs;
  pyproject = false;
  src = ./.;
  strictDeps = true;
  version = "0.0.0";
}

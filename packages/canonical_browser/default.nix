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
  icons =
    pkgs.runCommand "canonical-browser-icons"
      {
        nativeBuildInputs = [
          pkgs.gnutar
          pkgs.gzip
          python
        ];
      }
      ''
        mkdir -p lucide simple "$out/share/licenses/lucide" "$out/share/licenses/simple-icons" "$out/nix-support"
        tar -xzf ${lucideSource} --strip-components=1 -C lucide
        tar -xzf ${simpleIconsSource} --strip-components=1 -C simple
        python - <<'PYTHON'
        import json
        from pathlib import Path
        icons = {
            name: Path(f"lucide/icons/{name}.svg").read_text()
            for name in ("folder", "package", "monitor", "check", "play", "square")
        }
        for name, color in {"python": "3776ab", "html5": "e44d26", "nixos": "5277c3", "latex": "008080"}.items():
            icons[name] = Path(f"simple/icons/{name}.svg").read_text().replace("<svg ", f'<svg fill="#{color}" ')
        Path("icons.js").write_text("window.canonicalIcons = " + json.dumps(icons) + ";\n")
        PYTHON
        cp icons.js "$out/icons.js"
        cp lucide/LICENSE "$out/share/licenses/lucide/LICENSE"
        cp simple/LICENSE.md "$out/share/licenses/simple-icons/LICENSE"
        printf 'export CANONICAL_BROWSER_ICONS=%s/icons.js\n' "$out" > "$out/nix-support/setup-hook"
      '';
  lucideSource = pkgs.fetchurl {
    hash = "sha256-7pl9WqhrEzQhVqVKRQrbl685EwRjQApirHF309uLjLQ=";
    url = "https://registry.npmjs.org/lucide-static/-/lucide-static-0.563.0.tgz";
  };
  pname = baseNameOf ./.;
  python = pkgs.python3;
  runtimeInputs = [
    inputs.self.packages.${pkgs.stdenv.system}.canonical
    pkgs.diffoscope
    pkgs.git
    pkgs.nix
  ];
  simpleIconsSource = pkgs.fetchurl {
    hash = "sha256-EeGrfCX9CsvwAUxKiHRcM/4PhzQebZTHuh5iM0stI3U=";
    url = "https://registry.npmjs.org/simple-icons/-/simple-icons-16.33.0.tgz";
  };
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
    icons
    pkgs.makeWrapper
  ];
  passthru = {
    checkInputs = [ python.pkgs.httpx2 ];
    g6 = g6;
    icons = icons;
    python = python;
  };
  postFixup = ''
    cp ${icons}/icons.js "$out/${python.sitePackages}/$pname/prm/icons.js"
    cp ${g6}/g6.js "$out/${python.sitePackages}/$pname/prm/g6.js"
    mkdir -p "$out/share/licenses"
    cp -R ${icons}/share/licenses/. "$out/share/licenses/"
    cp -R ${g6}/share/licenses/g6 "$out/share/licenses/"
    wrapProgram "$out/bin/${pname}" --prefix PATH : "${pkgs.lib.makeBinPath runtimeInputs}"
  '';
  propagatedBuildInputs = runtimeInputs ++ [
    python.pkgs.fastapi
    python.pkgs.uvicorn
  ];
  pyproject = false;
  src = ./.;
  strictDeps = true;
  version = "0.0.0";
}

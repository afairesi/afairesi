{ inputs, pkgs, ... }:
let
  pname = baseNameOf ./.;
  python = pkgs.python3;
  runtimeInputs = [
    inputs.self.packages.${pkgs.stdenv.system}.git-canonical
    pkgs.bash
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
    description = "Minimal interactive coding agent for a local llama.cpp server";
    mainProgram = pname;
  };
  nativeBuildInputs = [ pkgs.makeWrapper ];
  passthru.python = python;
  postFixup = ''
    wrapProgram "$out/bin/${pname}" --prefix PATH : "${pkgs.lib.makeBinPath runtimeInputs}"
  '';
  propagatedBuildInputs = runtimeInputs;
  pyproject = false;
  src = ./.;
  strictDeps = true;
  version = "0.0.0";
}

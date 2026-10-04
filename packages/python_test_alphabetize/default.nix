{ pkgs, ... }:
let
  pname = baseNameOf ./.;
  python = pkgs.python3;
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
    description = "Sort Python test definitions alphabetically within their scopes";
    mainProgram = pname;
  };
  passthru.python = python;
  propagatedBuildInputs = [ python.pkgs.libcst ];
  pyproject = false;
  src = ./.;
  strictDeps = true;
  version = "0.0.0";
}

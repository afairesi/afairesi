arguments@{
  pkgs,
  src,
  executable,
  ...
}:
let
  python = pkgs.python3;
  cliName = baseNameOf src;
  pname = builtins.replaceStrings [ "-" ] [ "_" ] cliName;
in
python.pkgs.buildPythonPackage (
  {
    version = "0.0.0";
    propagatedBuildInputs = [ ];
  }
  // builtins.removeAttrs arguments [
    "pkgs"
    "executable"
  ]
  // {
    inherit pname src;
    installPhase = ''
      install -Dm644 main.py "$out/${python.sitePackages}/$pname/__init__.py"
      ${pkgs.lib.optionalString executable ''
        mkdir -p "$out/bin"
        printf '%s\n' '#!${python.interpreter}' "from $pname import main" 'main()' > "$out/bin/${cliName}"
        chmod 755 "$out/bin/${cliName}"
      ''}
      if [ -d prm ]; then
        cp -R prm/ "$out/${python.sitePackages}/$pname/"
      fi
      ${python.interpreter} -m compileall -q --invalidation-mode unchecked-hash \
        "$out/${python.sitePackages}/$pname/__init__.py"
    '';
    meta =
      builtins.removeAttrs (arguments.meta or { }) [ "mainProgram" ]
      // pkgs.lib.optionalAttrs executable { mainProgram = cliName; };
    passthru = (arguments.passthru or { }) // {
      inherit python;
    };
    pyproject = false;
    strictDeps = true;
  }
)

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
assert
  !(arguments ? shellHook)
  || throw "mkPythonPackage does not accept shellHook; use runtimeHook for app initialization";
assert
  !(arguments ? runtimeHook)
  || executable
  || throw "runtimeHook requires an executable package";
python.pkgs.buildPythonPackage (
  {
    version = "0.0.0";
    propagatedBuildInputs = [ ];
  }
  // builtins.removeAttrs arguments [
    "pkgs"
    "executable"
    "runtimeHook"
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
    } // pkgs.lib.optionalAttrs (arguments ? runtimeHook) { inherit (arguments) runtimeHook; };
    pyproject = false;
    strictDeps = true;
  }
)

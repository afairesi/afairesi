arguments@{
  pkgs,
  src,
  executable,
  ...
}:
let
  pname = baseNameOf src;
  module = "«${pname}»";
  leanDeps = arguments.leanDeps or [ ];
  library = "${pname}_lib";
  lakefile = (pkgs.formats.toml { }).generate "lakefile.toml" {
    name = pname;
    defaultTargets = [ (if executable then pname else library) ];
    lean_lib = [
      {
        name = library;
        roots = [ module ];
        defaultFacets = [ "static" ];
      }
    ];
    lean_exe = pkgs.lib.optional executable {
      name = pname;
      root = module;
    };
    require = map (dependency: {
      name = dependency.lakePackageName or dependency.pname;
      path = toString dependency;
    }) leanDeps;
  };
in
assert
  !(arguments ? shellHook)
  || throw "mkLean4Package does not accept shellHook; use runtimeHook for app initialization";
assert
  !(arguments ? runtimeHook) || executable || throw "runtimeHook requires an executable package";
pkgs.leanPackages.buildLakePackage (
  {
    version = "0.0.0";
  }
  // builtins.removeAttrs arguments [
    "pkgs"
    "executable"
    "runtimeHook"
  ]
  // {
    inherit pname src leanDeps;
    leanPackageName = module;
    postPatch = ''
      ${arguments.postPatch or ""}
      cp ${lakefile} lakefile.toml
      ${pkgs.lib.optionalString (
        pname != "main"
      ) "cp main.lean ${pkgs.lib.escapeShellArg "${pname}.lean"}"}
      if [ -d prm ]; then
        cp -R prm ${pkgs.lib.escapeShellArg pname}
      fi
    '';
    meta =
      builtins.removeAttrs (arguments.meta or { }) [ "mainProgram" ]
      // pkgs.lib.optionalAttrs executable { mainProgram = pname; };
    passthru =
      (arguments.passthru or { })
      // pkgs.lib.optionalAttrs (arguments ? runtimeHook) { inherit (arguments) runtimeHook; };
  }
)

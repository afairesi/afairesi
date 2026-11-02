{ flake, candidates }:
let
  configurations = flake.nixosConfigurations or { };
  names = builtins.attrNames configurations;
  lib = flake.inputs.nixpkgs.lib;
  location = candidate: "${flake.outPath}/${candidate.file}";
  definitions =
    configuration: candidate:
    (lib.attrByPath candidate.path { } configuration.options).definitionsWithLocations or [ ];
  consumes =
    configuration: candidate:
    builtins.any (definition: toString (definition.file or "") == location candidate) (
      definitions configuration candidate
    );
  eligible =
    candidate:
    let
      matches = builtins.filter (name: consumes configurations.${name} candidate) names;
    in
    matches != [ ]
    && builtins.all (
      name:
      let
        configuration = configurations.${name};
        option = lib.attrByPath candidate.path { } configuration.options;
      in
      configuration ? extendModules
      && builtins.length (definitions configuration candidate) == 1
      && option ? default
      && builtins.toJSON candidate.value == builtins.toJSON option.default
    ) matches;
  omit =
    path: attributes:
    let
      name = builtins.head path;
      rest = builtins.tail path;
      nested = omit rest attributes.${name};
    in
    if rest == [ ] then
      builtins.removeAttrs attributes [ name ]
    else if nested == { } then
      builtins.removeAttrs attributes [ name ]
    else
      attributes // { ${name} = nested; };
  replacement =
    removals: file:
    args@{ config, ... }:
    let
      original = import file;
      parameters = builtins.mapAttrs (name: _: args.${name} or config._module.args.${name}) (
        builtins.functionArgs original
      );
      value = if builtins.isFunction original then original (args // parameters) else original;
      stripped = builtins.foldl' (
        attributes: candidate:
        omit ((if attributes ? config then [ "config" ] else [ ]) ++ candidate.path) attributes
      ) value (builtins.filter (candidate: location candidate == file) removals);
    in
    stripped
    // {
      _file = file;
      key = "${file}:afairesi-omitted";
    };
  preserves =
    removals:
    builtins.all (
      name:
      let
        configuration = configurations.${name};
        relevant = builtins.filter (consumes configuration) removals;
        files = lib.unique (map location relevant);
        updated = configuration.extendModules {
          modules = [
            {
              disabledModules = files;
              imports = map (replacement relevant) files;
            }
          ];
        };
      in
      relevant == [ ]
      || builtins.all (
        candidate:
        !consumes updated candidate
        &&
          builtins.toJSON (
            lib.attrByPath candidate.path (throw "missing original option") configuration.config
          ) == builtins.toJSON (lib.attrByPath candidate.path (throw "missing omitted option") updated.config)
      ) relevant
    ) names;
  attempt =
    removals: candidate:
    let
      proposed = removals ++ [ candidate ];
      checked = builtins.tryEval (eligible candidate && preserves proposed);
    in
    if checked.success && checked.value then proposed else removals;
in
builtins.seq names (
  map (candidate: { inherit (candidate) file path; }) (builtins.foldl' attempt [ ] candidates)
)

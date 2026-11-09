{ candidates, flake }:
let
  attempt =
    removals: candidate:
    let
      checked = builtins.tryEval (eligible candidate && preserves proposed);
      proposed = removals ++ [ candidate ];
    in
    if checked.success && checked.value then proposed else removals;
  configurations = flake.nixosConfigurations or { };
  consumes =
    configuration: candidate:
    builtins.any (definition: toString (definition.file or "") == location candidate) (
      definitions configuration candidate
    );
  definitions =
    configuration: candidate:
    (lib.attrByPath candidate.path { } configuration.options).definitionsWithLocations or [ ];
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
  lib = flake.inputs.nixpkgs.lib;
  location = candidate: "${flake.outPath}/${candidate.file}";
  names = builtins.attrNames configurations;
  omit =
    path: attributes:
    let
      name = builtins.head path;
      nested = omit rest attributes.${name};
      rest = builtins.tail path;
    in
    if rest == [ ] then
      builtins.removeAttrs attributes [ name ]
    else if nested == { } then
      builtins.removeAttrs attributes [ name ]
    else
      attributes // { ${name} = nested; };
  preserves =
    removals:
    builtins.all (
      name:
      let
        configuration = configurations.${name};
        files = lib.unique (map location relevant);
        relevant = builtins.filter (consumes configuration) removals;
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
  replacement =
    removals: file:
    args@{ config, ... }:
    let
      original = import file;
      parameters = builtins.mapAttrs (name: _: args.${name} or config._module.args.${name}) (
        builtins.functionArgs original
      );
      stripped = builtins.foldl' (
        attributes: candidate:
        omit ((if attributes ? config then [ "config" ] else [ ]) ++ candidate.path) attributes
      ) value (builtins.filter (candidate: location candidate == file) removals);
      value = if builtins.isFunction original then original (args // parameters) else original;
    in
    stripped
    // {
      _file = file;
      key = "${file}:afairesi-omitted";
    };
in
builtins.seq names (
  map (candidate: { inherit (candidate) file path; }) (builtins.foldl' attempt [ ] candidates)
)

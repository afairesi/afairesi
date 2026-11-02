{ pkgs, package }:
pkgs.runCommand "remove-nix-defaults-modules"
  {
    nativeBuildInputs = [
      pkgs.nix
      package
    ];
  }
  ''
    export HOME="$TMPDIR/home"
    export NIX_REMOTE="local?root=$TMPDIR/store"
    export NIX_CONFIG=$'experimental-features = nix-command flakes\nbuild-users-group =\n'
    mkdir -p "$HOME" source
    cd source
    cat > flake.nix <<'NIX'
    {
      inputs.nixpkgs.url = "path:${pkgs.path}";
      outputs = { nixpkgs, ... }:
        let
          modules = [
            ./configuration.nix
            ({ lib, options, ... }: {
              options = {
                flag = lib.mkOption { type = lib.types.bool; default = false; };
                redundant = lib.mkOption { type = lib.types.bool; default = false; };
                payload = lib.mkOption { type = lib.types.str; };
                first = lib.mkOption {
                  type = lib.types.bool;
                  default = options.second.highestPrio != 100;
                };
                second = lib.mkOption {
                  type = lib.types.bool;
                  default = options.first.highestPrio != 100;
                };
              };
              config._module.args.payload = "preserved module argument";
            })
          ];
        in {
        nixosConfigurations = {
          ordinary = nixpkgs.lib.evalModules { inherit modules; };
          overridden = nixpkgs.lib.evalModules {
            modules = modules ++ [({ lib, ... }: { flag = lib.mkDefault true; })];
          };
        };
      };
    }
    NIX
    cat > configuration.nix <<'NIX'
    { payload, ... }: {
      imports = [ ./imported.nix ];
      config = {
        inherit payload;
        flag = false;
        redundant = false;
        first = false;
        second = false;
      };
    }
    NIX
    printf '%s\n' '{}' > imported.nix
    nix eval --json .#nixosConfigurations --apply 'builtins.mapAttrs (_: host: host.config)' > before.json
    remove_nix_defaults .
    nix eval --json .#nixosConfigurations --apply 'builtins.mapAttrs (_: host: host.config)' > after.json
    cmp before.json after.json
    grep -F 'flag = false;' configuration.nix
    grep -F 'second = false;' configuration.nix
    if grep -E 'redundant =|first =' configuration.nix; then
      exit 1
    fi
    cp configuration.nix stable.nix
    remove_nix_defaults .
    cmp configuration.nix stable.nix
    mkdir -p "$out"
  ''

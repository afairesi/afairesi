afairesiInputs:
let
  formatter =
    { pkgs, self }:
    let
      inherit (pkgs.stdenv.hostPlatform) system;
    in
    mkTreefmtTool {
      inherit pkgs self;
      module = {
        enableDefaultExcludes = false;
        programs = {
          oxfmt = {
            enable = true;
            priority = 5;
          };
          ruff-format = {
            enable = true;
            priority = 5;
          };
          shfmt = {
            enable = true;
            indent_size = 2;
          };
          terraform = {
            enable = true;
            includes = [ "*.tf" ];
          };
          texfmt = {
            enable = true;
            priority = 5;
          };
        };
        settings.formatter = {
          alphabetize-nix = {
            command = pkgs.writeShellApplication {
              name = "format-nix";
              runtimeInputs = [
                afairesiInputs.self.packages.${system}.alphabetize_nix
                afairesiInputs.self.packages.${system}.uncomment
                pkgs.coreutils
                pkgs.diffutils
                pkgs.nixfmt
              ];
              text = ''
                workspace=$(mktemp -d)
                trap 'rm -rf "$workspace"' EXIT
                for source in "$@"; do
                  if [ -L "$source" ]; then
                    continue
                  fi
                  temporary="$workspace/$(basename "$source")"
                  cp "$source" "$temporary"
                  nixfmt - < "$temporary" > /dev/null
                  uncomment --config ${pkgs.writeText "uncomment.toml" ""} --no-gitignore "$temporary"
                  alphabetize_nix "$temporary"
                  nixfmt "$temporary"
                  if ! cmp -s "$source" "$temporary"; then
                    cat "$temporary" > "$source"
                  fi
                done
              '';
            };
            includes = [ "*.nix" ];
            priority = 5;
          };
          oxfmt.options = [
            "--config"
            "${pkgs.writeText "oxfmt.json" (
              builtins.toJSON {
                endOfLine = "lf";
                insertFinalNewline = true;
                printWidth = 100;
                singleQuote = false;
                tabWidth = 2;
                useTabs = false;
              }
            )}"
            "--disable-nested-config"
            "--ignore-path"
            "/dev/null"
          ];
          ruff-format.options = [
            "--isolated"
            (ruffTarget pkgs)
          ];
          texfmt.options = [ "--noconfig" ];
          uncomment = {
            command = afairesiInputs.self.packages.${system}.uncomment;
            includes = [
              "*.py"
              "*.pyi"
              "*.html"
              "*.js"
              "*.jsx"
              "*.mjs"
              "*.cjs"
              "*.ts"
              "*.tsx"
              "*.css"
              "*.scss"
              "*.vue"
              "*.yaml"
              "*.yml"
              "*.jsonc"
              "*.json5"
              "*.tex"
              "*.sty"
              "*.cls"
            ];
            options = [
              "--config"
              "${pkgs.writeText "uncomment.toml" ""}"
              "--no-gitignore"
            ];
            priority = 1;
          };
        }
        // pkgs.lib.genAttrs' (packageNamesFor self pkgs "lean4") (name: {
          name = "lean4-${name}";
          value = {
            command = mkLean4Tool {
              inherit pkgs;
              packageDrv = self.packages.${system}.${name};
              packageName = name;
            };
            includes = [ "${name}/*.lean" ];
          };
        });
      };
    };
  leanFmt =
    { pkgs }:
    pkgs.leanPackages.buildLakePackage {
      isLibrary = false;
      meta.mainProgram = "lean-fmt";
      pname = "lean-fmt";
      src = pkgs.fetchFromGitHub {
        hash = "sha256-jIgeAE5km36uFre/EPymroFNSt2ixM7v7qrM13QBD7g=";
        owner = "jcreinhold";
        repo = "lean-fmt";
        rev = "v4.35.0";
      };
      version = "4.35.0";
    };
  lint =
    { pkgs, self }:
    let
      inherit (pkgs.stdenv.hostPlatform) system;
      pythonPackageNames = packageNamesFor self pkgs "python";
      pythonSources =
        name:
        builtins.filter (path: builtins.pathExists (self.outPath + "/${path}")) [
          "${name}/main.py"
          "${name}/test_main.py"
        ];
      tflintConfig = pkgs.writeText "tflint.hcl" ''
        plugin "terraform" {
          enabled = true
          preset  = "all"
        }
      '';
    in
    mkTreefmtTool {
      inherit pkgs self;
      module = {
        programs = {
          mypy = {
            directories = pkgs.lib.genAttrs pythonPackageNames (
              name:
              let
                environment = mkTestEnvironment {
                  inherit pkgs;
                  packageDrv = self.packages.${system}.${name};
                };
              in
              {
                directory = "";
                modules = pythonSources name;
                options = [
                  "--config-file=${pkgs.writeText "mypy.ini" "[mypy]\n"}"
                  "--python-executable=${environment.python}/bin/python"
                  "--no-namespace-packages"
                  "--ignore-missing-imports"
                  "--strict"
                ];
              }
            );
            enable = true;
          };
          shellcheck = {
            enable = true;
            package = pkgs.writeShellApplication {
              name = "shellcheck";
              runtimeInputs = [ pkgs.shellcheck ];
              text = ''
                for file in "$@"; do
                  IFS= read -r -n 2 prefix < "$file" || true
                  config=--norc
                  if [[ $prefix != '#!' ]]; then
                    config=--rcfile=${pkgs.writeText "shellcheckrc" "shell=bash\n"}
                  fi
                  shellcheck "$config" -- "$file"
                done
              '';
            };
          };
          yamllint = {
            enable = true;
            settings.extends = "default";
          };
        };
        settings.formatter = {
          deadnix = {
            command = pkgs.deadnix;
            includes = [ "*.nix" ];
            options = [ "--fail" ];
          };
          oxlint = {
            command = pkgs.oxlint;
            includes = [
              "*.js"
              "*.jsx"
              "*.mjs"
              "*.cjs"
              "*.ts"
              "*.tsx"
            ];
            options = [
              "--config"
              "${pkgs.writeText "oxlint.json" "{}"}"
              "--disable-nested-config"
              "--no-ignore"
              "-D"
              "all"
              "-A"
              "unicorn/empty-brace-spaces"
            ];
          };
          ruff-check = {
            command = pkgs.ruff;
            includes = [
              "*.py"
              "*.pyi"
            ];
            options = [
              "check"
              "--isolated"
              (ruffTarget pkgs)
              "--no-fix"
              "--extend-select=ALL"
              "--extend-ignore=W191,E111,E114,E117,D203,D206,D300,Q000,Q001,Q002,Q003,Q004,COM812,COM819"
              "--per-file-ignores=test_main.py:INP001"
            ];
          };
          statix = {
            command = pkgs.writeShellApplication {
              name = "statix-check";
              runtimeInputs = [ pkgs.statix ];
              text = ''
                for file in "$@"; do
                  statix check --config ${pkgs.writeText "statix.toml" ""} --unrestricted "$file"
                done
              '';
            };
            includes = [ "*.nix" ];
          };
          tflint = {
            command = pkgs.writeShellApplication {
              name = "tflint-check";
              runtimeInputs = [
                pkgs.coreutils
                pkgs.tflint
              ];
              text = ''
                declare -A checked
                for file in "$@"; do
                  directory=$(dirname -- "$file")
                  if [[ ! -v 'checked[$directory]' ]]; then
                    tflint --chdir="$directory" --config=${tflintConfig} --minimum-failure-severity=notice
                    checked[$directory]=1
                  fi
                done
              '';
            };
            includes = [ "*.tf" ];
          };
        }
        // pkgs.lib.genAttrs' pythonPackageNames (name: {
          name = "mypy-${name}";
          value.includes = pkgs.lib.mkForce [ "${name}/*.py" ];
        })
        // pkgs.lib.genAttrs' (packageNamesFor self pkgs "lean4") (name: {
          name = "lean4-${name}";
          value = {
            command = mkLean4Tool {
              inherit pkgs;
              check = true;
              packageDrv = self.packages.${system}.${name};
              packageName = name;
            };
            includes = [ "${name}/*.lean" ];
          };
        });
      };
    };
  mkBashPackage =
    arguments@{ pkgs, src, ... }:
    pkgs.writeShellApplication (
      builtins.removeAttrs arguments [
        "pkgs"
        "src"
      ]
      // {
        name = baseNameOf src;
        passthru = (arguments.passthru or { }) // {
          afairesiKind = "bash";
          runtimeInputs = arguments.runtimeInputs or [ ];
        };
        text = builtins.readFile (src + "/main.sh");
      }
    );
  mkFlake =
    {
      inputs,
      nixpkgs ? { },
      systems ? [
        "aarch64-darwin"
        "aarch64-linux"
        "x86_64-linux"
      ],
    }:
    let
      inherit (repositoryInputs) self;
      bashNames = system: packageNamesFor self packageSets.${system} "bash";
      discover =
        marker:
        builtins.attrNames (
          lib.filterAttrs (
            name: type: type == "directory" && builtins.pathExists (source + "/${name}/${marker}")
          ) (builtins.readDir source)
        );
      environments = lib.genAttrs systems (
        system:
        lib.genAttrs (namesFor system) (
          name:
          mkTestEnvironment {
            packageDrv = self.packages.${system}.${name};
            pkgs = packageSets.${system};
          }
        )
      );
      hostsFor =
        system:
        lib.filterAttrs (_: configuration: configuration.pkgs.stdenv.hostPlatform.system == system) (
          self.nixosConfigurations or { }
        );
      lean4Names = system: packageNamesFor self packageSets.${system} "lean4";
      lib = repositoryInputs.nixpkgs.lib;
      mergeChecks =
        groups:
        builtins.foldl' (
          result: group:
          assert lib.assertMsg (
            builtins.intersectAttrs result group == { }
          ) "Afairesi generated check names collide";
          result // group
        ) { } groups;
      namesFor = system: bashNames system ++ pythonNames system ++ lean4Names system;
      packageNames = discover "default.nix";
      packageSets = lib.genAttrs systems (
        system:
        if (nixpkgs.config or { }) == { } && (nixpkgs.overlays or [ ]) == [ ] then
          repositoryInputs.nixpkgs.legacyPackages.${system}
        else
          import repositoryInputs.nixpkgs {
            config = nixpkgs.config or { };
            localSystem.system = system;
            overlays = nixpkgs.overlays or [ ];
          }
      );
      pythonNames = system: packageNamesFor self packageSets.${system} "python";
      repositoryInputs = {
        inherit (afairesiInputs) nixpkgs;
      }
      // inputs;
      source = self.outPath;
      supported =
        system: lib.filterAttrs (_: lib.meta.availableOn packageSets.${system}.stdenv.hostPlatform);
      testedNames =
        system: kind: filename:
        builtins.filter (name: builtins.pathExists (source + "/${name}/${filename}")) (
          packageNamesFor self packageSets.${system} kind
        );
    in
    {
      apps = lib.genAttrs systems (
        system:
        lib.mapAttrs
          (name: package: {
            meta = {
              description = "Run ${name}";
            }
            // (package.meta or { });
            program = toString (
              packageSets.${system}.writeShellScript "${name}-run" ''
                set -e
                ${package.runtimeHook}
                exec ${lib.getExe package} "$@"
              ''
            );
            type = "app";
          })
          (
            lib.filterAttrs (
              _: package: package ? runtimeHook && package.runtimeHook != ""
            ) self.packages.${system}
          )
      );
      checks = lib.genAttrs systems (
        system:
        mergeChecks (
          [
            (lib.genAttrs (testedNames system "bash" "test_main.sh") (
              name:
              packageSets.${system}.runCommand name environments.${system}.${name}.environment ''
                cp -R ${source + "/${name}"} source
                chmod -R u+w source
                cd source
                ${packageSets.${system}.bash}/bin/bash -euo pipefail test_main.sh
                touch "$out"
              ''
            ))
            (lib.genAttrs (testedNames system "generic" "test_main.nix") (
              pname:
              lib.callPackageWith {
                inherit system pname;
                inputs = repositoryInputs;
                packageDrv = self.packages.${system}.${pname};
                pkgs = packageSets.${system};
              } (source + "/${pname}/test_main.nix") { }
            ))
            (lib.genAttrs (testedNames system "lean4" "test_main.lean") (
              name:
              mkLean4Check {
                packageDrv = self.packages.${system}.${name};
                packageName = name;
                pkgs = packageSets.${system};
                testEnvironment = environments.${system}.${name};
              }
            ))
            (lib.genAttrs (testedNames system "python" "test_main.py") (
              name:
              pythonCheck {
                packageDrv = self.packages.${system}.${name};
                packageName = name;
                pkgs = packageSets.${system};
                testEnvironment = environments.${system}.${name};
              }
            ))
            (lib.mapAttrs' (
              host: _:
              lib.nameValuePair "${host}Vm" (mkHostCheck {
                inherit host;
                inputs = repositoryInputs;
                pkgs = packageSets.${system};
              })
            ) (hostsFor system))
            (lib.mapAttrs' (
              name: configuration: lib.nameValuePair "nixos-${name}" configuration.config.system.build.toplevel
            ) (hostsFor system))
            (lib.mapAttrs' (name: shell: lib.nameValuePair "devshell-${name}" shell) self.devShells.${system})
            {
              formatter = self.formatter.${system}.passthru.tests.check;
              lint =
                (lint {
                  inherit self;
                  pkgs = packageSets.${system};
                }).passthru.tests.check;
              structure = sourceCheck {
                pkgs = packageSets.${system};
                src = source;
              };
            }
          ]
          ++ lib.mapAttrsToList (
            name: package:
            {
              "pkgs-${name}" = package;
            }
            // lib.mapAttrs' (testName: test: lib.nameValuePair "pkgs-${name}-${testName}" test) (
              supported system (package.passthru.tests or { })
            )
          ) (builtins.intersectAttrs (lib.genAttrs packageNames (_: null)) self.packages.${system})
        )
      );
      devShells = lib.genAttrs systems (
        system:
        lib.mapAttrs' (name: environment: {
          name = "${name}-test";
          value = environment.shell;
        }) environments.${system}
      );
      formatter = lib.genAttrs systems (
        system:
        formatter {
          inherit self;
          pkgs = packageSets.${system};
        }
      );
      nixosConfigurations = lib.genAttrs (discover "configuration.nix") (
        hostName:
        repositoryInputs.nixpkgs.lib.nixosSystem {
          modules = [
            (source + "/${hostName}/configuration.nix")
            (
              { config, lib, ... }:
              lib.optionalAttrs ((nixpkgs.config or { }) != { } || (nixpkgs.overlays or [ ]) != [ ]) {
                nixpkgs.pkgs = lib.mkDefault packageSets.${config.nixpkgs.hostPlatform.system};
              }
            )
          ];
          specialArgs = {
            inherit hostName;
            inputs = repositoryInputs;
          };
        }
      );
      packages = lib.genAttrs systems (
        system:
        let
          moduleNames = map (name: builtins.replaceStrings [ "-" ] [ "_" ] name) (
            builtins.attrNames (
              lib.filterAttrs (_: package: (package.afairesiKind or "generic") == "python") packages
            )
          );
          packages = supported system (
            lib.genAttrs packageNames (
              pname:
              lib.callPackageWith {
                inherit system pname;
                inputs = repositoryInputs;
                pkgs = packageSets.${system};
              } (source + "/${pname}/default.nix") { }
            )
          );
        in
        assert lib.assertMsg (
          builtins.length (lib.unique moduleNames) == builtins.length moduleNames
        ) "Afairesi Python packages have colliding installed module names";
        packages
      );
    };
  mkHostCheck =
    {
      host,
      inputs,
      pkgs,
    }:
    let
      inherit (pkgs) lib;
      configuration = inputs.self.nixosConfigurations.${host};
      hasDisko = builtins.attrNames (configuration.config.disko.devices or { }) != [ ];
      instrumented = configuration.extendModules {
        modules = [
          (
            if hasDisko then
              {
                virtualisation.vmVariantWithDisko.virtualisation.graphics = lib.mkForce false;
              }
            else
              {
                virtualisation.vmVariant.virtualisation.graphics = lib.mkForce false;
              }
          )
          ({ modulesPath, ... }: {
            imports = [ (modulesPath + "/testing/test-instrumentation.nix") ];
            users.users.root.initialHashedPassword = lib.mkForce null;
          })
        ];
      };
      name = "${host}Vm";
      startScript = pkgs.writeShellScript "start-${name}" (
        if hasDisko then
          ''
            set -e
            ${pkgs.util-linux}/bin/setsid ${vm}/bin/disko-vm "$@" &
            vm_pid=$!
            trap 'kill -- -"$vm_pid" 2>/dev/null || true; wait "$vm_pid" 2>/dev/null || true' EXIT
            trap 'exit 130' INT
            trap 'exit 143' TERM
            wait "$vm_pid"
          ''
        else
          ''
            exec ${vm}/bin/run-*-vm "$@"
          ''
      );
      vm = if hasDisko then vmConfig.system.build.vmWithDisko else vmConfig.system.build.vm;
      vmConfig =
        if hasDisko then
          instrumented.config.virtualisation.vmVariantWithDisko
        else
          instrumented.config.virtualisation.vmVariant;
    in
    pkgs.testers.runNixOSTest {
      inherit name;
      globalTimeout = 600;
      nodes = { };
      requiredFeatures.kvm = pkgs.stdenv.hostPlatform.isLinux;
      testScript = ''
        machine = create_machine(start_command="${startScript}", name="machine")
        driver.machines_qemu.append(machine)
        machine.start()
        machine.wait_for_unit("local-fs.target")
        machine.wait_for_unit("multi-user.target")
        machine.shutdown()
      '';
    };
  mkHtmlPackage =
    arguments@{ pkgs, src, ... }:
    let
      pname = baseNameOf src;
      site =
        pkgs.runCommand "${pname}-site"
          {
            postInstall = arguments.postInstall or "";
          }
          ''
            mkdir -p "$out"
            for asset in ${pkgs.lib.escapeShellArgs (builtins.attrNames (builtins.readDir src))}; do
              case "$asset" in
                default.nix|test_main.*|.gitignore|.git) continue ;;
              esac
              cp -R "${src}/$asset" "$out/$asset"
            done
            chmod -R u+w "$out"
            runHook postInstall
          '';
    in
    pkgs.writeShellApplication (
      {
        runtimeInputs = [
          pkgs.http-server
        ]
        ++ pkgs.lib.optionals pkgs.stdenv.hostPlatform.isLinux [ pkgs.xdg-utils ];
      }
      // builtins.removeAttrs arguments [
        "pkgs"
        "src"
        "postInstall"
      ]
      // {
        name = pname;
        passthru = (arguments.passthru or { }) // {
          afairesiKind = "html";
        };
        text = ''
          open_args=()
          if [[ -n "''${DISPLAY:-}" || -n "''${WAYLAND_DISPLAY:-}" ]]; then
            open_args=(-o /)
          fi
          server_args=()
          for argument in "$@"; do
            case "$argument" in
              --no-open)
                open_args=()
                ;;
              -o|--o|-o=*|--o=*|--no-o)
                open_args=()
                server_args+=("$argument")
                ;;
              -h|--help)
                printf '%s\n' 'Desktop runs open the browser; --no-open disables this.'
                server_args+=("$argument")
                ;;
              *)
                server_args+=("$argument")
                ;;
            esac
          done
          exec http-server ${site} "''${open_args[@]}" "''${server_args[@]}"
        '';
      }
    );
  mkLatexPackage =
    arguments@{ pkgs, src, ... }:
    pkgs.stdenv.mkDerivation (
      {
        nativeBuildInputs = [ pkgs.texliveFull ];
        version = "0.0.0";
      }
      // builtins.removeAttrs arguments [ "pkgs" ]
      // {
        buildPhase = ''
          latexmk -pdf ms.tex
        '';
        installPhase = ''
          install -Dm644 ms.pdf "$out/ms.pdf"
        '';
        passthru = (arguments.passthru or { }) // {
          afairesiKind = "latex";
        };
        pname = baseNameOf src;
        strictDeps = true;
      }
    );
  mkLean4Check =
    {
      packageDrv,
      packageName,
      pkgs,
      testEnvironment,
    }:
    pkgs.runCommand packageName
      (
        {
          buildInputs = [ packageDrv ];
          nativeBuildInputs = testEnvironment.dependencies ++ [ testEnvironment.lean4 ];
        }
        // pkgs.lib.optionalAttrs (packageDrv.meta ? mainProgram) {
          PACKAGE_E2E_EXECUTABLE = pkgs.lib.getExe packageDrv;
        }
      )
      ''
        cp -R ${packageDrv} source
        chmod -R u+w source
        cd source
        lake lean test_main.lean
        mkdir -p "$out"
      '';
  mkLean4Package =
    arguments@{
      executable,
      pkgs,
      src,
      ...
    }:
    let
      lakefile = (pkgs.formats.toml { }).generate "lakefile.toml" {
        inherit leanOptions;
        defaultTargets = [ (if executable then pname else library) ];
        lean_exe = pkgs.lib.optional executable {
          name = pname;
          root = module;
        };
        lean_lib = [
          {
            defaultFacets = [ "static" ];
            name = library;
            roots = [ module ];
          }
        ];
        name = pname;
        require = map (dependency: {
          name = dependency.lakePackageName or dependency.pname;
          path = toString dependency;
        }) leanDeps;
        srcDir = ".lake/src";
      };
      leanDeps = arguments.leanDeps or [ ];
      leanOptions = {
        autoImplicit = false;
        linter = {
          all = true;
          coreInternal.internalModule = false;
        };
        warningAsError = true;
      };
      library = "${pname}_lib";
      module = "«${pname}»";
      pname = baseNameOf src;
    in
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
        inherit pname leanDeps;
        leanPackageName = module;
        meta =
          builtins.removeAttrs (arguments.meta or { }) [ "mainProgram" ]
          // pkgs.lib.optionalAttrs executable { mainProgram = pname; };
        passthru =
          (arguments.passthru or { })
          // {
            afairesiKind = "lean4";
          }
          // pkgs.lib.optionalAttrs (arguments ? runtimeHook) { inherit (arguments) runtimeHook; };
        postBuild = ''
          ${arguments.postBuild or ""}
          lake --wfail lint --builtin-only
        '';
        postPatch = ''
          ${arguments.postPatch or ""}
          cp ${lakefile} lakefile.toml
          mkdir -p .lake/src
          ln -s ../../main.lean ${pkgs.lib.escapeShellArg ".lake/src/${pname}.lean"}
          ln -s ../.. ${pkgs.lib.escapeShellArg ".lake/src/${pname}"}
        '';
      }
    );
  mkLean4Tool =
    {
      check ? false,
      packageDrv,
      packageName,
      pkgs,
    }:
    let
      leanPath = pkgs.lib.makeSearchPath ".lake/build/lib/lean" (
        pkgs.lib.concatMap (
          dependency: [ dependency ] ++ (dependency.allLeanDeps or [ ])
        ) testEnvironment.dependencies
      );
      testEnvironment = mkTestEnvironment { inherit pkgs packageDrv; };
    in
    pkgs.writeShellApplication {
      name = "lean4-${if check then "check" else "format"}-${packageName}";
      runtimeInputs = testEnvironment.dependencies ++ [
        (leanFmt { inherit pkgs; })
        packageDrv.lean4
        pkgs.coreutils
        pkgs.diffutils
      ];
      text = ''
        source_root="$PWD/${packageName}"
        workspace=$(mktemp -d)
        trap 'rm -rf "$workspace"' EXIT
        cp -R ${packageDrv}/. "$workspace"
        chmod -R u+w "$workspace"
        printf '%s\n' 'leanprover/lean4:v${packageDrv.lean4.version}' > "$workspace/lean-toolchain"
        files=()
        ${pkgs.lib.optionalString (!check) "declare -A original_hashes=()"}
        for source in "$@"; do
          relative=$(realpath --relative-to="$source_root" "$source")
          mkdir -p "$(dirname "$workspace/$relative")"
          cp "$source" "$workspace/$relative"
          ${pkgs.lib.optionalString (!check) ''
            original_hashes["$relative"]=$(sha256sum "$workspace/$relative" | cut -d ' ' -f1)
          ''}
          files+=("$relative")
        done
        export LEAN_PATH=${pkgs.lib.escapeShellArg leanPath}
        export LEAN_SYSROOT=${packageDrv.lean4}
        export LEAN_CC=${pkgs.stdenv.cc}/bin/cc
        export LAKE_NO_CACHE=1
        ${pkgs.lib.optionalString (packageDrv.meta ? mainProgram) ''
          export PACKAGE_E2E_EXECUTABLE=${pkgs.lib.getExe packageDrv}
        ''}
      ''
      + (
        if check then
          ''
            lean-fmt check --root "$workspace" --no-cache --select all "''${files[@]}"
          ''
        else
          ''
            format_status=0
            lean-fmt format --root "$workspace" --no-cache "''${files[@]}" || format_status=$?
            for relative in "''${files[@]}"; do
              if ! cmp -s "$workspace/$relative" "$source_root/$relative"; then
                current_hash=$(sha256sum "$source_root/$relative" | cut -d ' ' -f1) || current_hash=""
                if [ "$current_hash" != "''${original_hashes[$relative]}" ]; then
                  printf 'File changed while formatting; rerun formatter: %s\n' "$source_root/$relative" >&2
                  format_status=1
                  continue
                fi
                cp "$workspace/$relative" "$source_root/$relative"
              fi
            done
            exit "$format_status"
          ''
      );
    };
  mkPythonPackage =
    arguments@{
      executable,
      pkgs,
      src,
      ...
    }:
    let
      cliName = baseNameOf src;
      pname = builtins.replaceStrings [ "-" ] [ "_" ] cliName;
      python = pkgs.python3;
    in
    assert
      !(builtins.pathExists (src + "/__init__.py"))
      || throw "mkPythonPackage reserves __init__.py for main.py; move initialization into main.py";
    assert
      (
        builtins.match "[a-zA-Z_][a-zA-Z0-9_]*" pname != null
        && !(builtins.elem pname [
          "False"
          "None"
          "True"
          "and"
          "as"
          "assert"
          "async"
          "await"
          "break"
          "class"
          "continue"
          "def"
          "del"
          "elif"
          "else"
          "except"
          "finally"
          "for"
          "from"
          "global"
          "if"
          "import"
          "in"
          "is"
          "lambda"
          "nonlocal"
          "not"
          "or"
          "pass"
          "raise"
          "return"
          "try"
          "while"
          "with"
          "yield"
        ])
      )
      || throw "mkPythonPackage requires a valid non-keyword Python module name: ${pname}";
    assert
      !(arguments ? runtimeHook) || executable || throw "runtimeHook requires an executable package";
    python.pkgs.buildPythonPackage (
      {
        version = "0.0.0";
      }
      // builtins.removeAttrs arguments [
        "pkgs"
        "executable"
        "runtimeHook"
      ]
      // {
        inherit pname;
        installPhase = ''
          runHook preInstall
          install -Dm644 main.py "$out/${python.sitePackages}/$pname/__init__.py"
          ${pkgs.lib.optionalString executable ''
            mkdir -p "$out/bin"
            printf '%s\n' '#!${python.interpreter}' "from $pname import main" 'main()' > "$out/bin/${cliName}"
            chmod 755 "$out/bin/${cliName}"
          ''}
          for asset in ${pkgs.lib.escapeShellArgs (builtins.attrNames (builtins.readDir src))}; do
            case "$asset" in
              main.py|default.nix|test_main.py|.gitignore|.git) continue ;;
            esac
            cp -R "$asset" "$out/${python.sitePackages}/$pname/"
          done
          ${python.interpreter} -m compileall -q --invalidation-mode unchecked-hash \
            "$out/${python.sitePackages}/$pname/__init__.py"
          runHook postInstall
        '';
        meta =
          builtins.removeAttrs (arguments.meta or { }) [ "mainProgram" ]
          // pkgs.lib.optionalAttrs executable { mainProgram = cliName; };
        passthru =
          (arguments.passthru or { })
          // {
            afairesiKind = "python";
          }
          // pkgs.lib.optionalAttrs (arguments ? runtimeHook) { inherit (arguments) runtimeHook; };
        pyproject = false;
        strictDeps = true;
      }
    );
  mkTestEnvironment =
    { packageDrv, pkgs }:
    let
      dependencies = pkgs.lib.concatMap (name: packageDrv.${name} or [ ]) [
        "buildInputs"
        "checkInputs"
        "nativeBuildInputs"
        "nativeCheckInputs"
        "propagatedBuildInputs"
        "propagatedNativeBuildInputs"
      ];
      python = packageDrv.pythonModule.withPackages (
        ps:
        dependencies
        ++ [
          packageDrv
          ps.hypothesis
          ps.pytest
        ]
      );
    in
    {
      bash =
        let
          environment = {
            PACKAGE_E2E_EXECUTABLE = pkgs.lib.getExe packageDrv;
            nativeBuildInputs =
              dependencies
              ++ packageDrv.runtimeInputs
              ++ [
                packageDrv
                pkgs.bash
              ];
          };
        in
        {
          inherit environment;
          shell = pkgs.mkShell environment;
        };
      lean4 = {
        inherit dependencies;
        inherit (packageDrv) lean4;
        shell = pkgs.mkShell {
          buildInputs = [ packageDrv ];
          inputsFrom = [ packageDrv ];
          packages = dependencies ++ [ packageDrv.lean4 ];
        };
      };
      python = {
        inherit dependencies python;
        shell = pkgs.mkShell {
          inputsFrom = [ packageDrv ];
          packages = dependencies ++ [ python ];
        };
      };
    }
    .${packageDrv.afairesiKind};
  mkTreefmtTool =
    {
      module,
      pkgs,
      self,
    }:
    let
      inherit (afairesiInputs.treefmt-nix.lib.evalModule pkgs module) config;
      tool = pkgs.writeShellApplication {
        name = "treefmt";
        passthru.tests.check =
          pkgs.runCommandLocal "treefmt-check"
            {
              meta.description = "Check the project tree with treefmt";
              nativeBuildInputs = [
                pkgs.git
                pkgs.git-lfs
              ];
            }
            ''
              export HOME="$TMPDIR"
              export LC_ALL=${if pkgs.stdenv.hostPlatform.isDarwin then "en_US.UTF-8" else "C.UTF-8"}
              cp -R ${self.outPath} project
              chmod -R u+w project
              cd project
              git init --quiet
              git add --force -- .
              ${pkgs.lib.getExe tool} --no-cache
              git diff --exit-code
              touch "$out"
            '';
        runtimeInputs = [ pkgs.git ];
        text = ''
          unset PRJ_ROOT
          exec ${pkgs.lib.getExe config.package} --config-file=${config.build.configFile} \
            --tree-root-file=flake.nix --walk git "$@"
        '';
      };
    in
    tool;
  packageNamesFor =
    self: pkgs: kind:
    builtins.attrNames (
      pkgs.lib.filterAttrs (
        name: package:
        builtins.pathExists (self.outPath + "/${name}/default.nix")
        && (package.afairesiKind or "generic") == kind
      ) self.packages.${pkgs.stdenv.hostPlatform.system}
    );
  pythonCheck =
    {
      packageDrv,
      packageName,
      pkgs,
      testEnvironment,
    }:
    let
      inherit (packageDrv) pythonModule;
      installedModule = "${packageDrv}/${pythonModule.sitePackages}/${packageDrv.pname}";
      pytestPlugin = pkgs.writeTextDir "_afairesi_coverage_support.py" ''
        import os
        from collections.abc import Generator
        from pathlib import Path

        import coverage
        import pytest
        from hypothesis import settings


        @pytest.hookimpl(tryfirst=True)
        def pytest_load_initial_conftests() -> None:
            """Set reproducible Hypothesis defaults before loading project tests."""
            settings.register_profile("afairesi", max_examples=100, derandomize=True)
            settings.load_profile("afairesi")


        def remap_coverage_paths(installed_path: str) -> None:
            """Combine installed-module coverage with the corresponding source file."""
            data = coverage.CoverageData()
            data.read()
            mapped = coverage.CoverageData(basename=".coverage-mapped")
            mapped.update(
                data,
                map_path=lambda path: (
                    os.environ["src"] + "/main.py"
                    if path == installed_path
                    else path
                ),
            )
            mapped.write()
            Path(mapped.data_filename()).replace(data.data_filename())


        @pytest.hookimpl(wrapper=True)
        def pytest_runtest_protocol(item: pytest.Item) -> Generator[None, object, object]:
            """Attribute test setup, execution, teardown, and children to their test."""
            previous = os.environ.get("AFAIRESI_TEST_CONTEXT", "")
            os.environ["AFAIRESI_TEST_CONTEXT"] = item.nodeid
            tracer = coverage.Coverage.current()
            if tracer is not None:
                tracer.switch_context(item.nodeid)
            try:
                return (yield)
            finally:
                os.environ["AFAIRESI_TEST_CONTEXT"] = previous
                if tracer is not None:
                    tracer.switch_context(previous)
      '';
      startup = pkgs.writeTextDir "sitecustomize.py" ''
        import coverage
        coverage.process_startup()
      '';
    in
    pkgs.runCommand "${packageName}-coverage"
      (
        {
          inherit (packageDrv) src;
          nativeBuildInputs = testEnvironment.dependencies ++ [ testEnvironment.python ];
        }
        // pkgs.lib.optionalAttrs (packageDrv.meta ? mainProgram) {
          PACKAGE_E2E_EXECUTABLE = pkgs.lib.getExe packageDrv;
        }
      )
      ''
        mkdir -p "$out"
        export COVERAGE_PROCESS_START="$TMPDIR/coverage.ini"
        unset AFAIRESI_TEST_CONTEXT
        cat > "$COVERAGE_PROCESS_START" <<EOF
        [run]
        core = ctrace
        context = \$AFAIRESI_TEST_CONTEXT
        parallel = true
        branch = true
        data_file = $out/.coverage
        source =
            $src
            ${installedModule}
        omit =
            */test_main.py
        EOF
        export PYTHONPATH="${startup}:${pytestPlugin}:${pythonModule.pkgs.coverage}/${pythonModule.sitePackages}:$PYTHONPATH"
        export HOME="$(mktemp -d)"
        cd "$out"
        "${testEnvironment.python}/bin/python" -m pytest -p _afairesi_coverage_support -p no:cacheprovider \
          --import-mode=importlib --durations=10 --maxfail=1 "$src/test_main.py"
        export COVERAGE_RCFILE="$COVERAGE_PROCESS_START"
        unset COVERAGE_PROCESS_START
        python -m coverage combine
        python -c 'from _afairesi_coverage_support import remap_coverage_paths; remap_coverage_paths("${installedModule}/__init__.py")'
        python -m coverage html --show-contexts -d "$out/html"
        python -m coverage json --show-contexts -o "$out/coverage.json"
      '';
  ruffTarget =
    pkgs: "--target-version=py${builtins.replaceStrings [ "." ] [ "" ] pkgs.python3.pythonVersion}";
  sourceCheck =
    { pkgs, src }:
    let
      python = pkgs.python3.withPackages (_: [
        afairesiInputs.self.packages.${pkgs.stdenv.hostPlatform.system}.afairesi
      ]);
    in
    pkgs.runCommand "afairesi-structure-check" { } ''
      ${python}/bin/python - "${src}" <<'PY'
      import sys
      from pathlib import Path
      from afairesi import CommandError, validate_flake_source

      try:
          validate_flake_source(Path(sys.argv[1]).resolve())
      except (CommandError, OSError, SyntaxError, ValueError) as error:
          sys.exit(f"error: {error}")
      PY
      touch "$out"
    '';
in
{
  inherit lint;
  lib = {
    inherit
      mkBashPackage
      mkFlake
      mkHtmlPackage
      mkLatexPackage
      mkLean4Package
      mkPythonPackage
      mkTestEnvironment
      ;
  };
}

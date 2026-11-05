afairesiInputs:
let
  formatter =
    {
      enableMypy ? true,
      inputs,
      pkgs,
      self ? inputs.self,
    }:
    let
      formatterPython = pkgs.python3.withPackages (_: [
        inputs.self.packages.${pkgs.stdenv.system}.afairesi
      ]);
      lean4PackageNames = packageNamesFor "main.lean";
      nixFiles = [
        "flake.nix"
        "*/default.nix"
        "*/main.nix"
        "*/test_main.nix"
        "*/configuration.nix"
        "*/hardware-configuration.nix"
      ];
      packageNamesFor =
        marker:
        builtins.attrNames (
          pkgs.lib.filterAttrs (
            name: _:
            builtins.pathExists (self.outPath + "/${name}/default.nix")
            && builtins.pathExists (self.outPath + "/${name}/${marker}")
          ) self.packages.${pkgs.stdenv.system}
        );
      pythonFiles = [
        "*/main.py"
        "*/test_main.py"
      ];
      pythonPackageNames = if enableMypy then packageNamesFor "main.py" else [ ];
      rawFormatter = treefmtEval.config.build.wrapper;
      treefmtEval = inputs.treefmt-nix.lib.evalModule pkgs {
        enableDefaultExcludes = false;
        programs = {
          actionlint = {
            enable = true;
            includes = workflowFiles;
            priority = 6;
          };
          deadnix = {
            enable = true;
            includes = nixFiles;
            priority = 3;
          };
          mypy = {
            directories = pkgs.lib.genAttrs pythonPackageNames (
              name:
              let
                environment = inputs.self.lib.mkTestEnvironment {
                  inherit pkgs;
                  packageDrv = self.packages.${pkgs.stdenv.system}.${name};
                };
              in
              {
                directory = "";
                modules = [ "${name}" ];
                options = [
                  "--python-executable=${environment.python}/bin/python"
                  "--cache-dir=${name}/tmp/.mypy_cache"
                  "--exclude=/(prm|tmp)/"
                  "--no-namespace-packages"
                  "--ignore-missing-imports"
                  "--strict"
                ];
              }
            );
            enable = enableMypy;
          };
          nixfmt = {
            enable = true;
            includes = nixFiles;
            priority = 5;
          };
          oxfmt = {
            enable = true;
            includes = workflowFiles ++ [
              "*/style.css"
              "*/index.html"
              "*/script.js"
            ];
            priority = 5;
          };
          ruff-check = {
            enable = true;
            extendSelect = [ "ALL" ];
            includes = pythonFiles;
            priority = 3;
          };
          ruff-format = {
            enable = true;
            includes = pythonFiles;
            priority = 5;
          };
          statix = {
            enable = true;
            includes = nixFiles;
            priority = 3;
          };
          texfmt = {
            enable = true;
            includes = [ "*/ms.tex" ];
            priority = 5;
          };
          yamllint = {
            enable = true;
            includes = workflowFiles;
            priority = 6;
          };
        };
        projectRootFile = "flake.nix";
        settings = {
          formatter = {
            alphabetize-nix = {
              command = inputs.self.packages.${pkgs.stdenv.system}.alphabetize_nix;
              includes = nixFiles;
              priority = 4;
            };
            bibtex-tidy = {
              command = pkgs.bibtex-tidy;
              includes = [ "*/ms.bib" ];
              options = [
                "--duplicates"
                "--no-align"
                "--no-wrap"
                "--sort"
                "--sort-fields"
                "--v2"
              ];
              priority = 5;
            };
            oxlint = {
              command = pkgs.oxlint;
              includes = [ "*/script.js" ];
              options = [
                "--fix-dangerously"
                "-D"
                "all"
              ];
              priority = 3;
            };
            remove-empty-lines = {
              command = inputs.self.packages.${pkgs.stdenv.system}.remove_empty_lines;
              includes = [ ".gitignore" ];
              priority = 2;
            };
            ruff-check.options = [
              "--cache-dir=tmp/.ruff_cache"
              "--per-file-ignores=test_main.py:INP001"
              "--unsafe-fixes"
            ];
            ruff-format.options = [ "--cache-dir=tmp/.ruff_cache" ];
            uncomment = {
              command = inputs.self.packages.${pkgs.stdenv.system}.uncomment;
              includes = [
                ".gitignore"
                "LICENSE"
                "flake.lock"
              ]
              ++ nixFiles
              ++ pythonFiles
              ++ [
                "*/index.html"
                "*/script.js"
                "*/style.css"
                "*/ms.tex"
                "*/ms.bib"
              ]
              ++ workflowFiles;
              priority = 1;
            };
          }
          // pkgs.lib.genAttrs (map (name: "mypy-${name}") pythonPackageNames) (_: {
            priority = 6;
          })
          // pkgs.lib.genAttrs' lean4PackageNames (name: {
            name = "lean4-${name}";
            value = {
              command = mkLean4Format {
                inherit pkgs;
                packageDrv = self.packages.${pkgs.stdenv.system}.${name};
                packageName = name;
              };
              includes = [ "${name}/*.lean" ];
              priority = 5;
            };
          });
          global.excludes = [ "{prm,tmp,*/prm,*/tmp}/**" ];
        };
      };
      workflowFiles = [
        ".github/workflows/workflow.yml"
        ".forgejo/workflows/workflow.yml"
      ];
      wrapper = pkgs.writeShellApplication {
        name = "treefmt";
        runtimeInputs = [
          pkgs.git
          pkgs.nix
        ];
        text = ''
          ${formatterPython}/bin/python -c 'from afairesi import formatter_main; formatter_main()'
          exec ${rawFormatter}/bin/treefmt "$@"
        '';
      };
    in
    wrapper
    // {
      passthru = wrapper.passthru // {
        raw = rawFormatter;
        tests.check = (treefmtEval.config.build.check self.outPath).overrideAttrs (previous: {
          buildCommand = ''
            ${formatterPython}/bin/python -c 'from afairesi import formatter_main; formatter_main()' --source ${self.outPath}
          ''
          + previous.buildCommand;
          nativeBuildInputs = (previous.nativeBuildInputs or [ ]) ++ [
            pkgs.git
            pkgs.nix
          ];
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
        owner = "jcreinhold";
        repo = "lean-fmt";
        rev = "v4.35.0";
        sha256 = "1f0g05sdgk5axvpwxi52vm54v0dflvy11gxp2sp7x6v49q01x24c";
      };
      version = "4.35.0";
    };
  mkCoverage =
    { check, packageDrv }:
    let
      context = builtins.toFile "coverage_context.py" (
        "from _afairesi_coverage_support import _coverage_context_plugin\n"
        + "pytest_runtest_protocol = _coverage_context_plugin()\n"
      );
      overrideCheck =
        check.overrideAttrs or (update: builtins.derivation (check.drvAttrs // update check));
      runtime = builtins.toFile "coverage_support.py" (
        "from __future__ import annotations\n"
        + builtins.elemAt (builtins.split "COVERAGE_RUNTIME_(START|END) = None\n" (
          builtins.readFile (afairesiInputs.self.outPath + "/afairesi/main.py")
        )) 2
      );
      startup = builtins.toFile "sitecustomize.py" ("import coverage\n" + "coverage.process_startup()\n");
    in
    overrideCheck (previous: {
      buildCommand = ''
        mkdir -p "$out/html" "$TMPDIR/coverage-startup"
        export COVERAGE_PROCESS_START="$TMPDIR/coverage.ini"
        afairesi_pytest_args=(-p _afairesi_coverage_context)
        unset AFAIRESI_TEST_CONTEXT
        cp "${context}" "$TMPDIR/coverage-startup/_afairesi_coverage_context.py"
        cp "${startup}" "$TMPDIR/coverage-startup/sitecustomize.py"
        cp "${runtime}" "$TMPDIR/coverage-startup/_afairesi_coverage_support.py"
        cat > "$COVERAGE_PROCESS_START" <<EOF
        [run]
        core = ctrace
        context = \$AFAIRESI_TEST_CONTEXT
        parallel = true
        branch = true
        data_file = $out/.coverage
        source =
            $src
            ${packageDrv}/${packageDrv.python.sitePackages}/${packageDrv.pname}
        omit =
            */test_main.py
            */prm/*
        EOF
        export PYTHONPATH="$TMPDIR/coverage-startup:$PWD:${packageDrv.python.pkgs.coverage}/${packageDrv.python.sitePackages}:$PYTHONPATH"
      ''
      + previous.buildCommand
      + ''
        unset COVERAGE_PROCESS_START
        python -m coverage combine --rcfile="$TMPDIR/coverage.ini"
        python -c 'from _afairesi_coverage_support import remap_coverage_paths; remap_coverage_paths("${packageDrv}/${packageDrv.python.sitePackages}/${packageDrv.pname}/__init__.py")'
        python -m coverage html --show-contexts --rcfile="$TMPDIR/coverage.ini" -d "$out/html"
        python -m coverage json --show-contexts --rcfile="$TMPDIR/coverage.ini" -o "$out/coverage.json"
      '';
      name = "${previous.name}-coverage";
    });
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
      discover =
        marker:
        builtins.attrNames (
          lib.filterAttrs (
            name: type:
            type == "directory"
            && !(builtins.elem name [
              "prm"
              "tmp"
            ])
            && builtins.pathExists (source + "/${name}/${marker}")
          ) (if builtins.pathExists source then builtins.readDir source else { })
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
      lean4Names = builtins.filter (
        name: builtins.pathExists (source + "/${name}/main.lean")
      ) packageNames;
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
      moduleNames = map (name: builtins.replaceStrings [ "-" ] [ "_" ] name) pythonNames;
      namesFor =
        system:
        builtins.filter (name: builtins.hasAttr name self.packages.${system}) (pythonNames ++ lean4Names);
      nixNames = builtins.filter (
        name:
        !(builtins.any (marker: builtins.pathExists (source + "/${name}/${marker}")) [
          "main.py"
          "main.lean"
          "index.html"
          "ms.tex"
        ])
      ) packageNames;
      packageNames = discover "default.nix";
      packageSets = lib.genAttrs systems (
        system:
        if (nixpkgs.config or { }) == { } && (nixpkgs.overlays or [ ]) == [ ] then
          repositoryInputs.nixpkgs.legacyPackages.${system}
        else
          import repositoryInputs.nixpkgs {
            inherit system;
            config = nixpkgs.config or { };
            overlays = nixpkgs.overlays or [ ];
          }
      );
      pythonNames = builtins.filter (
        name: builtins.pathExists (source + "/${name}/main.py")
      ) packageNames;
      repositoryInputs = {
        inherit (afairesiInputs) nixpkgs;
      }
      // inputs;
      source = self.outPath;
      supported =
        system:
        lib.filterAttrs (
          _: package:
          (package.meta.platforms or [ ]) == [ ]
          || lib.meta.availableOn packageSets.${system}.stdenv.hostPlatform package
        );
      testedLean4Names =
        system:
        builtins.filter (
          name: builtins.elem name lean4Names && builtins.pathExists (source + "/${name}/test_main.lean")
        ) (namesFor system);
      testedNames =
        system:
        builtins.filter (
          name: builtins.elem name pythonNames && builtins.pathExists (source + "/${name}/test_main.py")
        ) (namesFor system);
      testedNixNames =
        system:
        builtins.filter (
          name:
          builtins.hasAttr name self.packages.${system}
          && builtins.pathExists (source + "/${name}/test_main.nix")
        ) nixNames;
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
            (lib.genAttrs (testedNixNames system) (
              pname:
              lib.callPackageWith {
                inherit system pname;
                inputs = repositoryInputs;
                packageDrv = self.packages.${system}.${pname};
                pkgs = packageSets.${system};
              } (source + "/${pname}/test_main.nix") { }
            ))
            (lib.genAttrs (testedLean4Names system) (
              name:
              mkLean4Check {
                packageDrv = self.packages.${system}.${name};
                packageName = name;
                pkgs = packageSets.${system};
                testEnvironment = environments.${system}.${name};
              }
            ))
            (lib.genAttrs (testedNames system) (
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
              formatter = self.formatter.${system}.passthru.tests.check or self.formatter.${system};
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
          inputs = afairesiInputs;
          pkgs = packageSets.${system};
        }
      );
      legacyPackages = lib.genAttrs systems (
        system:
        lib.mapAttrs' (
          name: environment: lib.nameValuePair "${name}-test-environment" environment.manifest
        ) (lib.filterAttrs (_: environment: environment ? manifest) environments.${system})
        // lib.genAttrs' (testedNames system) (
          name:
          lib.nameValuePair "${name}-coverage" (mkCoverage {
            check = self.checks.${system}.${name};
            packageDrv = self.packages.${system}.${name};
          })
        )
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
            hostName = hostName;
            inputs = repositoryInputs;
          };
        }
      );
      packages = lib.genAttrs systems (
        system:
        assert lib.assertMsg (
          builtins.length (lib.unique moduleNames) == builtins.length moduleNames
        ) "Afairesi Python packages have colliding installed module names";
        supported system (
          lib.genAttrs packageNames (
            pname:
            lib.callPackageWith {
              inherit system pname;
              inputs = repositoryInputs;
              pkgs = packageSets.${system};
            } (source + "/${pname}/default.nix") { }
          )
        )
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
      preservedPaths = lib.concatLists (
        lib.mapAttrsToList (
          path: state:
          let
            directories = state.directories ++ lib.concatMap (user: user.directories) users;
            files = state.files ++ lib.concatMap (user: user.files) users;
            users = builtins.attrValues state.users;
          in
          map (file: {
            inherit (file) how;
            directory = false;
            path = file.file;
            persistent = path + file.file;
          }) files
          ++ map (directory: {
            inherit (directory) how;
            directory = true;
            path = directory.directory;
            persistent = path + directory.directory;
          }) (builtins.filter (directory: directory.how != "_intermediate") directories)
        ) (vmConfig.preservation.preserveAt or { })
      );
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
        import json
        import shlex
        machine = create_machine(start_command="${startScript}", name="machine")
        driver.machines_qemu.append(machine)
        machine.start(allow_reboot=True)
        for phase in ["boot", "reboot"]:
            with subtest(phase):
                machine.wait_for_unit("local-fs.target")
                machine.wait_for_unit("multi-user.target")
                for entry in json.loads(${builtins.toJSON (builtins.toJSON preservedPaths)}):
                    path = shlex.quote(entry["path"])
                    persistent = shlex.quote(entry["persistent"])
                    kind = "d" if entry["directory"] else "f"
                    machine.succeed(f"test -{kind} {path}; test -{kind} {persistent}")
                    if entry["how"] == "symlink":
                        assert machine.succeed(f"readlink -f {path}").strip() == machine.succeed(f"readlink -f {persistent}").strip()
                    else:
                        assert machine.succeed(f"stat -Lc '%d:%i' {path}").strip() == machine.succeed(f"stat -Lc '%d:%i' {persistent}").strip()
                    if entry["directory"]:
                        marker = shlex.quote(entry["path"] + "/.afairesi-preservation-probe")
                        if phase == "boot":
                            machine.succeed(f"printf afairesi-preserved > {marker}")
                        else:
                            assert machine.succeed(f"cat {marker}") == "afairesi-preserved"
            if phase == "boot":
                machine.reboot()
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
            cp ${src}/index.html "$out/index.html"
            for asset in script.js style.css; do
              if [ -f ${src}/"$asset" ]; then
                cp ${src}/"$asset" "$out/$asset"
              fi
            done
            if [ -d ${src}/prm ]; then
              cp -R ${src}/prm "$out/prm"
              chmod -R u+w "$out/prm"
            fi
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
        inherit src;
        buildPhase = ''
          latexmk -pdf ms.tex
        '';
        installPhase = ''
          install -Dm644 ms.pdf "$out/ms.pdf"
        '';
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
  mkLean4Format =
    {
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
      name = "lean4-format-${packageName}";
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
        declare -A original_hashes=()
        for source in "$@"; do
          relative=$(realpath --relative-to="$source_root" "$source")
          cp "$source" "$workspace/$relative"
          original_hashes["$relative"]=$(sha256sum "$workspace/$relative" | cut -d ' ' -f1)
          files+=("$relative")
        done
        export LEAN_PATH=${pkgs.lib.escapeShellArg leanPath}
        export LEAN_SYSROOT=${packageDrv.lean4}
        export LEAN_CC=${pkgs.stdenv.cc}/bin/cc
        export LAKE_NO_CACHE=1
        ${pkgs.lib.optionalString (packageDrv.meta ? mainProgram) ''
          export PACKAGE_E2E_EXECUTABLE=${pkgs.lib.getExe packageDrv}
        ''}
        format_status=0
        for relative in "''${files[@]}"; do
          (cd "$workspace" && lake env lean --run ${./prm/lean4-uncomment.lean} "$relative") || format_status=$?
        done
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
        if [ "$format_status" -ne 0 ]; then
          exit "$format_status"
        fi
        lean-fmt check --root "$workspace" --no-cache --select all "''${files[@]}"
      '';
    };
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
        meta =
          builtins.removeAttrs (arguments.meta or { }) [ "mainProgram" ]
          // pkgs.lib.optionalAttrs executable { mainProgram = pname; };
        passthru =
          (arguments.passthru or { })
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
          if [ -d prm ]; then
            ln -s ../../prm ${pkgs.lib.escapeShellArg ".lake/src/${pname}"}
          fi
        '';
      }
    );
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
      !(arguments ? shellHook)
      || throw "mkPythonPackage does not accept shellHook; use runtimeHook for app initialization";
    assert
      !(arguments ? runtimeHook) || executable || throw "runtimeHook requires an executable package";
    python.pkgs.buildPythonPackage (
      {
        propagatedBuildInputs = [ ];
        version = "0.0.0";
      }
      // builtins.removeAttrs arguments [
        "pkgs"
        "executable"
        "runtimeHook"
      ]
      // {
        inherit pname src;
        installPhase = ''
          runHook preInstall
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
          runHook postInstall
        '';
        meta =
          builtins.removeAttrs (arguments.meta or { }) [ "mainProgram" ]
          // pkgs.lib.optionalAttrs executable { mainProgram = cliName; };
        passthru =
          (arguments.passthru or { })
          // {
            inherit python;
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
      python = packageDrv.python.withPackages (
        ps:
        dependencies
        ++ [
          ps.hypothesis
          ps.pytest
        ]
      );
    in
    if packageDrv ? lean4 then
      {
        inherit dependencies;
        inherit (packageDrv) lean4;
        shell = pkgs.mkShell {
          buildInputs = [ packageDrv ];
          inputsFrom = [ packageDrv ];
          packages = dependencies ++ [ packageDrv.lean4 ];
        };
      }
    else
      {
        inherit dependencies python;
        manifest = pkgs.writeText "test-environment.json" (
          builtins.toJSON {
            path = pkgs.lib.makeBinPath dependencies;
            python = "${python}/bin/python";
          }
        );
        shell = pkgs.mkShell {
          inputsFrom = [ packageDrv ];
          packages = dependencies ++ [ python ];
        };
      };
  pythonCheck =
    {
      packageDrv,
      packageName,
      pkgs,
      testEnvironment,
    }:
    pkgs.runCommand packageName
      (
        (packageDrv.checkEnv or { })
        // {
          inherit (packageDrv) src;
          nativeBuildInputs = testEnvironment.dependencies ++ [ testEnvironment.python ];
        }
        // pkgs.lib.optionalAttrs (packageDrv.meta ? mainProgram) {
          PACKAGE_E2E_EXECUTABLE = pkgs.lib.getExe packageDrv;
        }
      )
      ''
        export HOME="$(mktemp -d)"
        mkdir -p "$out"
        ln -s "$src" "${builtins.replaceStrings [ "-" ] [ "_" ] packageName}"
        export PYTHONPATH="$PWD:$PYTHONPATH"
        cd "$out"
        "${testEnvironment.python}/bin/python" -c '
        import sys
        import pytest
        class Defaults:
            @pytest.hookimpl(tryfirst=True)
            def pytest_load_initial_conftests(self, early_config, parser, args):
                from hypothesis import settings
                settings.register_profile("afairesi", max_examples=10)
                settings.load_profile("afairesi")
        sys.exit(pytest.main(plugins=[Defaults()]))
        ' -p no:cacheprovider --import-mode=importlib --durations=10 --maxfail=1 \
          "$src/test_main.py" "''${afairesi_pytest_args[@]}"
      '';
in
{
  inherit formatter pythonCheck;
  lib = {
    mkCoverage = mkCoverage;
    mkFlake = mkFlake;
    mkHtmlPackage = mkHtmlPackage;
    mkLatexPackage = mkLatexPackage;
    mkLean4Package = mkLean4Package;
    mkPythonPackage = mkPythonPackage;
    mkTestEnvironment = mkTestEnvironment;
  };
}

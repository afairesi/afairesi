{ inputs, pkgs }:
let
  accepts =
    attributes:
    (builtins.tryEval (
      inputs.self.lib.mkPythonPackage (
        {
          inherit pkgs;
          executable = true;
          src = ./fixtures/example;
        }
        // attributes
      )
    )).success;
  acceptsLean =
    attributes:
    (builtins.tryEval (
      inputs.self.lib.mkLean4Package (
        {
          inherit pkgs;
          executable = true;
          src = ./fixtures/lean-example;
        }
        // attributes
      )
    )).success;
  consumer = inputs.self.lib.mkFlake {
    inputs = {
      afairesi = inputs.self;
      self = consumerSelf;
    };
    nixpkgs = {
      config = {
        allowUnfree = true;
        cudaSupport = true;
      };
      overlays = [ (_: _: { afairesiContract = true; }) ];
    };
    systems = pkgs.lib.unique [
      pkgs.stdenv.hostPlatform.system
      "x86_64-linux"
    ];
  };
  consumerSelf = consumer // {
    outPath = consumerSource;
    packages = builtins.mapAttrs (
      _: packages: packages // { default = packages.example; }
    ) consumer.packages;
  };
  consumerSource = ./fixtures;
  empty = inputs.self.lib.mkFlake {
    inputs.self = empty // {
      outPath = consumerSource + "/unavailable";
    };
    systems = [ system ];
  };
  failedCheck = pkgs.testers.testBuildFailure' {
    drv = overriddenCheck.overrideAttrs {
      src = pkgs.writeTextDir "test_main.py" ''
        from hypothesis import given, settings, strategies as st

        @given(st.just(0))
        def test_failure(value):
            assert settings.default.max_examples == 100
            assert settings.default.derandomize
            assert value != 0, "generated assertion failed"
      '';
    };
    expectedBuilderLogEntries = [
      "FAILED"
      "test_failure"
      "AssertionError: generated assertion failed"
    ];
    name = "hypothesis-failure";
  };
  htmlPackage = inputs.self.lib.mkHtmlPackage {
    inherit pkgs;
    meta.description = "Source listing";
    postInstall = ''
      printf '%s\n' '<a href="src/main.rs">Source</a>' > "$out/index.html"
    '';
    runtimeInputs = [ (pkgs.writeShellScriptBin "http-server" ''printf '%s\0' "$@"'') ];
    src = ./fixtures/rust-native;
  };
  latexPackage = inputs.self.lib.mkLatexPackage {
    inherit pkgs;
    passthru.manuscript = ./fixtures/example/ms.tex;
    src = ./fixtures/example;
  };
  leanExample = consumer.packages.${system}.lean-example;
  leanLibrary = consumer.packages.${system}.test_main;
  lintTool = (import ./main.nix inputs).lint {
    inherit pkgs;
    self = consumerSelf;
  };
  overridden = inputs.self.lib.mkFlake {
    inputs = {
      afairesi = inputs.self;
      self = overridden // {
        packages.${system}.example = overriddenPackage;
        outPath = consumerSource;
      };
    };
    systems = [ system ];
  };
  overriddenCheck = overridden.checks.${system}.example;
  overriddenEnvironment = inputs.self.lib.mkTestEnvironment {
    inherit pkgs;
    packageDrv = overriddenPackage;
  };
  overriddenPackage = package.overridePythonAttrs (attributes: {
    checkInputs = [ pkgs.python3Packages.six ];
    meta = (builtins.removeAttrs attributes.meta [ "description" ]) // {
      mainProgram = "renamed";
    };
    nativeCheckInputs = [ pkgs.python3Packages.colorama ];
    passthru = attributes.passthru // {
      inherit (pkgs) lean4;
      tests.unavailable = pkgs.runCommand "excluded-check" {
        meta.badPlatforms = [ system ];
      } "exit 1";
    };
    postInstall = ''mv "$out/bin/example" "$out/bin/renamed"'';
  });
  package = consumer.packages.${system}.example;
  pythonLibrary = inputs.self.lib.mkPythonPackage {
    inherit pkgs;
    executable = false;
    postInstall = ''
      test -d "$out/lib"
      printf post >> "$out/install-hooks"
    '';
    preInstall = ''
      test ! -e "$out/lib"
      mkdir -p "$out"
      printf pre > "$out/install-hooks"
    '';
    src = ./fixtures/lean-example;
  };
  system = pkgs.stdenv.hostPlatform.system;

in
assert
  package.consumerConfig == {
    afairesiContract = true;
    allowUnfree = true;
    cudaSupport = true;
  };
assert package.meta.mainProgram == "example";
assert package.afairesiKind == "python";
assert pythonLibrary.pname == "lean_example";
assert !(pythonLibrary.meta ? mainProgram);
assert consumer.packages.${system}.bash-example.afairesiKind == "bash";
assert htmlPackage.afairesiKind == "html";
assert htmlPackage.meta.description == "Source listing";
assert leanExample.afairesiKind == "lean4";
assert consumer.checks.${system} ? example;
assert leanExample.meta.mainProgram == "lean-example";
assert !(leanLibrary.meta ? mainProgram);
assert !(leanExample.drvAttrs ? runtimeHook);
assert !(consumer.apps.${system} ? test_main);
assert
  !(acceptsLean {
    executable = false;
    runtimeHook = "echo invalid";
  });
assert builtins.attrNames consumer.formatter == builtins.attrNames consumer.packages;
assert consumer.checks.${system}.formatter == consumer.formatter.${system}.passthru.tests.check;
assert pkgs.lib.all (
  name: consumer.checks.${system}."pkgs-${name}" == consumer.packages.${system}.${name}
) (builtins.attrNames consumer.packages.${system});
assert
  consumer.checks.${system}.pkgs-python_native-contract
  == consumer.packages.${system}.python_native.passthru.tests.contract;
assert !(consumer.packages.${system}.python_native ? afairesiKind);
assert !(consumer.packages.${system}.rust-native ? afairesiKind);
assert
  builtins.attrNames consumer.devShells.${system} == [
    "bash-example-test"
    "example-test"
    "lean-example-test"
    "test_main-test"
  ];
assert pkgs.lib.all (
  name: consumer.checks.${system}."devshell-${name}" == consumer.devShells.${system}.${name}
) (builtins.attrNames consumer.devShells.${system});
assert !(consumer.packages.${system} ? unavailable);
assert !(consumer.checks.${system} ? unavailable);
assert !(consumer.checks.${system} ? orphan);
assert !(consumer.checks.${system} ? nix-untested);
assert !(consumer.checks.${system} ? python_native);
assert consumer.packages.${system}.python_native.example == package;
assert consumer.nixosConfigurations.example.pkgs.config.allowUnfree;
assert consumer.nixosConfigurations.example.pkgs.config.cudaSupport;
assert consumer.nixosConfigurations.example.pkgs.afairesiContract;
assert consumer.nixosConfigurations.example.config.networking.hostName == "example";
assert (consumer.checks.${system} ? nixos-example) == (system == "x86_64-linux");
assert empty.packages.${system} == { };
assert builtins.attrNames empty.formatter == [ system ];
assert
  builtins.attrNames empty.checks.${system} == [
    "formatter"
    "lint"
    "structure"
  ];
assert empty.apps.${system} == { };
assert empty.devShells.${system} == { };
assert empty.nixosConfigurations == { };
assert (consumer.checks.${system} ? exampleVm) == (system == "x86_64-linux");
assert system != "x86_64-linux" || builtins.isString consumer.checks.${system}.exampleVm.drvPath;
assert consumer.apps.${system}.example.type == "app";
assert consumer.apps.${system}.example.meta == package.meta;
assert consumer.apps.${system}.example.meta.description == "Afairesi consumer contract example";
assert overridden.apps.${system}.example.meta.description == "Run example";
assert !(overridden.checks.${system} ? pkgs-example-unavailable);
assert overriddenCheck.PACKAGE_E2E_EXECUTABLE == "${overriddenPackage}/bin/renamed";
assert !(inputs.self.checks.${system}.nix_syntax ? PACKAGE_E2E_EXECUTABLE);
assert pkgs.lib.all
  (
    input:
    builtins.elem input overriddenCheck.nativeBuildInputs
    && builtins.elem input overriddenEnvironment.python.paths
  )
  [
    pkgs.python3Packages.six
    pkgs.python3Packages.colorama
  ];
assert !(package.drvAttrs ? runtimeHook);
assert !(accepts { src = ./fixtures/python_native; });
assert !(accepts { src = "/invalid/123"; });
assert !(accepts { src = "/invalid/class"; });
assert
  !(accepts {
    executable = false;
    runtimeHook = "echo invalid";
  });
assert !(inputs.self.apps.${system} ? afairesi);
assert inputs.self.packages.${system}.default == inputs.self.packages.${system}.afairesi;
pkgs.runCommand "afairesi-consumer-contract"
  {
    leanShell = consumer.devShells.${system}.lean-example-test;
    nativeBuildInputs = [
      overriddenCheck
      failedCheck
      consumer.checks.${system}.bash-example
      consumer.checks.${system}.pkgs-python_native-contract
      consumer.checks.${system}.pkgs-rust-native
      consumer.checks.${system}.nix-library
      consumer.checks.${system}.nix-tests
      consumer.checks.${system}.lean-example
      consumer.checks.${system}.test_main
      consumer.formatter.${system}
      pkgs.git
    ];
  }
  ''
    test -s ${overriddenCheck}/html/index.html
    ${pkgs.python3}/bin/python - <<'PY'
    import json
    from pathlib import Path

    source = Path("${package.src}/main.py")
    files = json.loads(Path("${overriddenCheck}/coverage.json").read_text())["files"]
    assert set(files) == {str(source)}
    measured = files[str(source)]
    lines = [line.strip() for line in source.read_text().splitlines()]
    assert measured["missing_lines"] == [lines.index("main()") + 1]
    assert measured["functions"]["main"]["summary"]["covered_branches"] == 2

    def contexts_at(statement):
        line = str(lines.index(statement) + 1)
        return {context.rsplit("/", 1)[-1] for context in measured["contexts"][line]}

    test_id = "test_main.py::test_executable_reports_the_inherited_runtime_hook_setting"
    unset = f"{test_id}[unset]"
    assert contexts_at('hook = "unloaded"') == {unset}
    assert contexts_at("if hook is None:") == {unset, f"{test_id}[set]"}
    PY
    head -c 5 ${latexPackage}/ms.pdf | grep -F '%PDF-'
    test ! -e ${pythonLibrary}/bin
    test "$(cat ${pythonLibrary}/install-hooks)" = prepost
    PYTHONPATH=${pythonLibrary}/${pkgs.python3.sitePackages} ${pkgs.python3}/bin/python -c 'import lean_example'
    ${pkgs.python3}/bin/python - <<'PY'
    import os
    import subprocess
    from pathlib import Path

    def launch(display="", wayland="", args=()):
        argv = subprocess.check_output(
            ["${pkgs.lib.getExe htmlPackage}", *args],
            env={**os.environ, "DISPLAY": display, "WAYLAND_DISPLAY": wayland},
        ).decode().split("\0")
        assert argv.pop() == ""
        return Path(argv[0]), argv[1:]

    site, options = launch()
    assert options == []
    assert (site / "index.html").read_text() == '<a href="src/main.rs">Source</a>\n'
    assert (site / "src/main.rs").read_bytes() == Path("${./fixtures/rust-native/src/main.rs}").read_bytes()
    assert launch(display=":0")[1] == launch(wayland="wayland-0")[1] == ["-o", "/"]
    assert launch(display=":0", args=["--no-open", "-p", "8761", "two words"])[1] == ["-p", "8761", "two words"]
    for args in (["-o", "/source"], ["--o=/source"], ["--no-o"]):
        assert launch(display=":0", args=args)[1] == args
    PY
    cmp ${latexPackage.manuscript} ${package}/${package.pythonModule.sitePackages}/example/ms.tex
    mkdir native-source
    cp -R ${consumerSource}/{python_native,rust-native} native-source/
    touch native-source/{flake.nix,flake.lock}
    ${pkgs.lib.getExe inputs.self.packages.${system}.afairesi} "$PWD/native-source" > native-report.json
    ${(pkgs.python3.withPackages (_: [ inputs.self.packages.${system}.afairesi ]))}/bin/python - <<'PY'
    import json
    from pathlib import Path
    from afairesi import validate_flake_source

    validate_flake_source(Path("native-source").resolve())
    report = json.loads(Path("native-report.json").read_text())
    assert set(report) == {"python_native", "rust-native"}
    for package in report.values():
        assert set(package) == {"type", "description"}
        assert package["type"] == "generic" and package["description"]
    PY
    test "$(cat ${consumer.packages.${system}.nix-untested})" = untested
    test "$(${pkgs.lib.getExe consumer.packages.${system}.rust-native})" = 42
    unset AFAIRESI_CONSUMER_HOOK
    expected=$(printf '%s\n' loaded "['two words', '--flag']")
    actual=$(${consumer.apps.${system}.example.program} 'two words' --flag)
    test "$actual" = "$expected"
    expected=$(printf '%s\n' unloaded "['two words', '--flag']")
    actual=$(${pkgs.lib.getExe package} 'two words' --flag)
    test "$actual" = "$expected"
    status=0
    AFAIRESI_CONSUMER_FAIL=1 ${consumer.apps.${system}.example.program} > failed-output || status=$?
    test "$status" = 23
    test ! -s failed-output
    expected=$(printf '%s\n' '42:resource:loaded' 'two words' '--flag')
    actual=$(${consumer.apps.${system}.lean-example.program} 'two words' --flag)
    test "$actual" = "$expected"
    test ! -e ${leanLibrary}/bin
    cmp ${leanLibrary.src}/test_main.lean ${leanLibrary}/test_main.lean
    cp -R ${consumerSource} formatted
    chmod -R u+w formatted
    cd formatted
    printf '{}\n' > flake.nix
    (cd example && ${lintTool}/bin/treefmt --no-cache --on-unmatched=fatal --formatters mypy-example main.py)
    for name in default orphan unavailable python_native; do
      if ${lintTool}/bin/treefmt --formatters "mypy-$name" flake.nix > selection.log 2>&1; then exit 1; fi
      grep -F "formatter mypy-$name not found in config" selection.log
      if treefmt --formatters "lean4-$name" flake.nix > selection.log 2>&1; then exit 1; fi
      grep -F "formatter lean4-$name not found in config" selection.log
    done
    mkdir -p supporting fixtures
    printf '%s\n' 'locals { value="hello" }' > supporting/main.tf
    if treefmt --no-cache --fail-on-change supporting/main.tf; then exit 1; fi
    grep -Fx 'locals { value = "hello" }' supporting/main.tf
    cat > pyproject.toml <<'TOML'
    [tool.ruff.lint]
    ignore = ["ALL"]
    TOML
    printf '%s\n' '{"rules":{"no-empty-function":"off"}}' > .oxlintrc.json
    printf '%s\n' 'supporting/' > .eslintignore
    printf '%s\n' '# Keep this explanation' 'import os' 'value=  1' > supporting/extra.py
    cp supporting/extra.py expected-extra.py
    if ${lintTool}/bin/treefmt --no-cache supporting/extra.py > lint-failure.log 2>&1; then
      exit 1
    fi
    grep -F F401 lint-failure.log
    printf '%s\n' 'export const empty = () => {};' > supporting/empty.js
    if ${lintTool}/bin/treefmt --no-cache supporting/empty.js > js-lint.log 2>&1; then
      exit 1
    fi
    grep -F no-empty-function js-lint.log
    cmp expected-extra.py supporting/extra.py
    cp supporting/extra.py fixtures/extra.py
    cp supporting/extra.py fixtures/source.py.txt
    cat > supporting/ordered.nix <<'NIX'
    # ordinary explanation
    {
      z = 2;
      a = 1;
      retained = false;
    }
    NIX
    printf '%s\n' '*' "" '!*/' > .gitignore
    printf '%s\n' first "" second > LICENSE
    printf '%s\n' '{' '"value": "first\nsecond",' '"items": [1, 2]' '}' > supporting/data.json
    cp supporting/data.json flake.lock
    cp flake.lock expected-lock
    git init --quiet
    git add --force .gitignore flake.lock LICENSE flake.nix
    treefmt --no-cache supporting/{extra.py,ordered.nix,data.json} fixtures/{extra.py,source.py.txt} flake.lock .gitignore LICENSE
    if grep -F '# Keep this explanation' supporting/extra.py; then exit 1; fi
    grep -Fx 'import os' supporting/extra.py
    grep -Fx 'value = 1' supporting/extra.py
    cmp supporting/extra.py fixtures/extra.py
    cmp expected-extra.py fixtures/source.py.txt
    touch -d @1 supporting/data.json supporting/ordered.nix
    treefmt --no-cache --fail-on-change supporting/{ordered.nix,data.json,main.tf} flake.lock .gitignore
    mkdir -p site/assets site/helpers paper
    printf '%s\n' '// ordinary explanation' 'export const value = () => { return "hello"; };' \
      'export const lines = `first' "" 'second`;' > site/script.js
    cp site/script.js site/assets/app.js
    printf '%s\n' '/* ordinary explanation */' 'body{color:red}' > site/style.css
    cp site/style.css site/assets/theme.css
    printf '%s\n' '<!doctype html><html><body><pre>first' "" 'second</pre></body></html>' > site/index.html
    printf '%s\n' '# ordinary explanation' 'import os' 'value=  "hello"' > site/helpers/extra.py
    printf '%s\n' '\documentclass{article}' '% ordinary explanation' '\begin{document}' 'Hello.' '\end{document}' > paper/ms.tex
    sources=(flake.nix site/{script.js,style.css,index.html,assets/app.js,assets/theme.css,helpers/extra.py} paper/ms.tex)
    git add --force -- "''${sources[@]}"
    cp expected-extra.py site/local.py
    index=$(git write-tree)
    treefmt --no-cache
    treefmt --no-cache --fail-on-change
    test "$(git write-tree)" = "$index"
    cmp expected-extra.py site/local.py
    cmp site/script.js site/assets/app.js
    cmp site/style.css site/assets/theme.css
    if grep -F 'ordinary explanation' "''${sources[@]}"; then exit 1; fi
    grep -Fx 'import os' site/helpers/extra.py
    git diff --binary -- "''${sources[@]}" > expected-format.diff

    printf '%s\n' '[tool.ruff.format]' 'quote-style = "single"' >> pyproject.toml
    printf '%s\n' '{"singleQuote":true,"semi":false}' > .oxfmtrc.json
    cp .oxfmtrc.json site/.oxfmtrc.json
    printf '%s\n' 'preserve_patterns = ["ordinary explanation"]' > .uncomment.toml
    printf '%s\n' 'wraplen = 10' > tex-fmt.toml
    printf '%s\n' 'root = true' '[*]' 'indent_style = tab' 'indent_size = 8' 'end_of_line = crlf' \
      'insert_final_newline = false' 'max_line_length = 20' > .editorconfig
    for config_state in local tracked; do
      if [[ $config_state == tracked ]]; then
        git add --force -- pyproject.toml .oxfmtrc.json site/.oxfmtrc.json .uncomment.toml tex-fmt.toml .editorconfig
      fi
      git restore --worktree -- "''${sources[@]}"
      treefmt --no-cache
      git diff --binary -- "''${sources[@]}" | cmp expected-format.diff -
    done
    ${pkgs.python3}/bin/python - <<'PY'
    from pathlib import Path
    import json
    source = Path("supporting/ordered.nix").read_text()
    assert "ordinary explanation" not in source
    assert source.index("a = 1;") < source.index("z = 2;")
    assert "retained = false;" in source
    assert Path(".gitignore").read_text() == "*\n\n!*/\n"
    assert Path("LICENSE").read_text() == "first\n\nsecond\n"
    assert "`first\n\nsecond`" in Path("site/script.js").read_text()
    assert "first\n\nsecond" in Path("site/index.html").read_text()
    assert Path("supporting/data.json").stat().st_mtime == 1
    assert Path("supporting/ordered.nix").stat().st_mtime == 1
    text = Path("supporting/data.json").read_text()
    assert text.endswith("\n")
    assert json.loads(text) == {"value": "first\nsecond", "items": [1, 2]}
    PY
    cmp expected-lock flake.lock
    cat > supporting/parse.js <<'JS'
    /* oxlint-disable eslint/no-empty */
    export const parse = (input) => {
      try { return JSON.parse(input); } catch {
        // ordinary explanation
      } finally {
        JSON.stringify(input);
      }
      return false;
    };
    JS
    treefmt --no-cache supporting/parse.js
    ${lintTool}/bin/treefmt --no-cache supporting/parse.js
    printf '%s\n' '{ broken = ; } # ordinary explanation' > supporting/broken.nix
    cp supporting/broken.nix expected-broken.nix
    if treefmt --no-cache supporting/broken.nix > broken-format.log 2>&1; then
      exit 1
    fi
    cmp expected-broken.nix supporting/broken.nix
    if ${lintTool}/bin/treefmt --no-cache fixtures/extra.py > fixture-lint.log 2>&1; then
      exit 1
    fi
    grep -F F401 fixture-lint.log
    cmp supporting/extra.py fixtures/extra.py
    printf '%s\n' '-- ordinary explanation' >> lean-example/main.lean
    sed -i 's/$/\r/' lean-example/main.lean
    mkdir -p lean-example/Nested
    printf '%s\n' '/-- A supporting constant. -/' 'def extra:Nat:=42' > lean-example/Nested/Extra.lean
    treefmt --no-cache {lean-example,test_main}/{main,test_main}.lean lean-example/Nested/Extra.lean
    treefmt --no-cache --fail-on-change {lean-example,test_main}/{main,test_main}.lean lean-example/Nested/Extra.lean
    printf '%s\n' '/-- A supporting constant. -/' 'def extra : Nat :=' '  42' | cmp - lean-example/Nested/Extra.lean
    grep -F -- '-- lean-fmt: ignore[FMT005]' lean-example/main.lean
    grep -F 'ordinary explanation' lean-example/main.lean
    grep -F 'set_option linter.missingDocs false in' test_main/main.lean
    cp test_main/main.lean expected-main.lean
    cp ${consumerSource}/test_main/main.lean test_main/main.lean
    printf '%s\n' 'import MissingFormatterDependency' > test_main/test_main.lean
    if treefmt --no-cache test_main/{main,test_main}.lean > format-failure.log 2>&1; then
      exit 1
    fi
    cmp expected-main.lean test_main/main.lean
    cp ${consumerSource}/test_main/main.lean test_main/main.lean
    cat > test_main/test_main.lean <<'LEAN'
    #eval show IO Unit from do
      let some target ← IO.getEnv "AFAIRESI_EDIT_TARGET"
        | throw (IO.userError "missing edit target")
      let marker := System.FilePath.mk (target ++ ".edited")
      unless ← marker.pathExists do
        IO.FS.writeFile marker ""
        IO.FS.writeFile target "-- concurrent edit\n"
    LEAN
    printf '%s\n' '-- concurrent edit' > expected-edit.lean
    status=0
    AFAIRESI_EDIT_TARGET="$PWD/test_main/main.lean" \
      treefmt --no-cache test_main/{main,test_main}.lean > concurrent-format.log 2>&1 || status=$?
    cmp expected-edit.lean test_main/main.lean
    test "$status" -ne 0
    mkdir -p $out
  ''

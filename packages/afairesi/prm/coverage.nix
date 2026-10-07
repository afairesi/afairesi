{
  check,
  packageDrv,
  packageName,
}:
let
  reportPlugin = ./test_report.py;
in
check.overrideAttrs (previous: {
  name = "${previous.name}-coverage";
  buildCommand = ''
    mkdir -p "$out/html" "$TMPDIR/coverage-startup"
    export COVERAGE_FILE="$out/.coverage"
    export COVERAGE_PROCESS_START="$TMPDIR/coverage.ini"
    export AFAIRESI_TEST_REPORT="$out/tests.json"
    export AFAIRESI_TEST_PACKAGE=${packageName}
    unset AFAIRESI_TEST_REPORT_OWNER AFAIRESI_MUTATION_REPORT AFAIRESI_TEST_CONTEXT
    cp "${reportPlugin}" "$TMPDIR/coverage-startup/_afairesi_test_report.py"
    export PYTEST_PLUGINS="_afairesi_test_report''${PYTEST_PLUGINS:+,$PYTEST_PLUGINS}"
    cat > "$COVERAGE_PROCESS_START" <<EOF
    [run]
    core = ctrace
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
    printf '%s\n' 'import os, coverage' 'tracer = coverage.Coverage.current() or coverage.process_startup()' 'if tracer is not None: tracer.switch_context(os.environ.get("AFAIRESI_TEST_CONTEXT", ""))' > "$TMPDIR/coverage-startup/sitecustomize.py"
    export PYTHONPATH="$TMPDIR/coverage-startup:$PWD:${packageDrv.python.pkgs.coverage}/${packageDrv.python.sitePackages}:$PYTHONPATH"
  ''
  + previous.buildCommand
  + ''
    unset COVERAGE_PROCESS_START
    python -m coverage combine --rcfile="$TMPDIR/coverage.ini"
    python - <<'PYTHON'
    import os
    import coverage
    data = coverage.CoverageData()
    data.read()
    mapped = coverage.CoverageData(basename=".coverage-mapped")
    installed = "${packageDrv}/${packageDrv.python.sitePackages}/${packageDrv.pname}/__init__.py"
    mapped.update(data, map_path=lambda path: os.environ["src"] + "/main.py" if path == installed else path)
    mapped.write()
    os.replace(mapped.data_filename(), data.data_filename())
    PYTHON
    python -m coverage html --show-contexts --rcfile="$TMPDIR/coverage.ini" -d "$out/html"
    python -m coverage json --show-contexts --rcfile="$TMPDIR/coverage.ini" -o "$out/coverage.json"
  '';
})

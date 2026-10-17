{
  check,
  packageDrv,
}:
let
  startup = builtins.toFile "sitecustomize.py" (
    "from _afairesi_coverage_support import start_subprocess_coverage\n"
    + "start_subprocess_coverage()\n"
  );
in
check.overrideAttrs (previous: {
  name = "${previous.name}-coverage";
  buildCommand = ''
    mkdir -p "$out/html" "$TMPDIR/coverage-startup"
    export COVERAGE_PROCESS_START="$TMPDIR/coverage.ini"
    afairesi_pytest_args=(-p _afairesi_coverage_context)
    unset AFAIRESI_TEST_CONTEXT
    cp "${./coverage_context.py}" "$TMPDIR/coverage-startup/_afairesi_coverage_context.py"
    cp "${startup}" "$TMPDIR/coverage-startup/sitecustomize.py"
    cp "${./coverage_support.py}" "$TMPDIR/coverage-startup/_afairesi_coverage_support.py"
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
})

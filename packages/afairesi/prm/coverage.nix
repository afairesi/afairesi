{
  check,
  packageDrv,
  packageName,
}:
let
  source = builtins.readFile ../main.py;
  testSupportSource =
    "from __future__ import annotations\n\ndef make_test_report_plugin"
    + builtins.head (
      builtins.split "\ndef python_test_support_source" (
        builtins.elemAt (builtins.split "\ndef make_test_report_plugin" source) 2
      )
    );
  reportPlugin = builtins.toFile "afairesi-test-report.py" (
    testSupportSource
    + "\nglobals().update(vars(make_test_report_plugin(${builtins.toJSON packageName})))\n"
  );
  startup = builtins.toFile "sitecustomize.py" (
    testSupportSource + "\nstart_subprocess_coverage()\n"
  );
  support = builtins.toFile "afairesi-test-support.py" testSupportSource;
in
check.overrideAttrs (previous: {
  name = "${previous.name}-coverage";
  buildCommand = ''
    mkdir -p "$out/html" "$TMPDIR/coverage-startup"
    export COVERAGE_PROCESS_START="$TMPDIR/coverage.ini"
    afairesi_pytest_args=(-p _afairesi_test_report)
    unset AFAIRESI_TEST_REPORT_OWNER AFAIRESI_TEST_CONTEXT
    cp "${reportPlugin}" "$TMPDIR/coverage-startup/_afairesi_test_report.py"
    cp "${startup}" "$TMPDIR/coverage-startup/sitecustomize.py"
    cp "${support}" "$TMPDIR/coverage-startup/_afairesi_test_support.py"
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
    python -c 'from _afairesi_test_support import remap_coverage_paths; remap_coverage_paths("${packageDrv}/${packageDrv.python.sitePackages}/${packageDrv.pname}/__init__.py")'
    python -m coverage html --show-contexts --rcfile="$TMPDIR/coverage.ini" -d "$out/html"
    python -m coverage json --show-contexts --rcfile="$TMPDIR/coverage.ini" -o "$out/coverage.json"
  '';
})

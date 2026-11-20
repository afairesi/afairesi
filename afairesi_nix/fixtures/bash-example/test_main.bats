@test "runtime dependencies are available to tests" {
  [[ "$(hello --greeting=ready)" == ready ]]
}

@test "the installed command preserves argument boundaries by name or path" {
  for executable in bash-example "$PACKAGE_E2E_EXECUTABLE"; do
    run "$executable" 'first arg' --flag
    [[ "$status" -eq 0 ]]
    [[ "$output" == $'two words\nfirst arg\n--flag' ]]
  done
}

expected=$'two words\nfirst arg\n--flag'
[[ "$(bash-example 'first arg' --flag)" == "$expected" ]]
[[ "$("$PACKAGE_E2E_EXECUTABLE" 'first arg' --flag)" == "$expected" ]]
[[ "$(hello --greeting=ready)" == ready ]]

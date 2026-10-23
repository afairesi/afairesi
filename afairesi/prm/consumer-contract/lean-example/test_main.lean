import «lean-example»

example : Proofs.answer = 42 := Proofs.answer_eq

#eval show IO Unit from do
  let greeting ← IO.Process.output { cmd := "hello" }
  unless greeting.exitCode == 0 && greeting.stdout == "Hello, world!\n" do
    throw (IO.userError s!"test dependency failed: {greeting.stderr}")
  let some executable ← IO.getEnv "PACKAGE_E2E_EXECUTABLE"
    | throw (IO.userError "missing executable")
  let result ← IO.Process.output { cmd := executable, args := #["two words", "--flag"] }
  unless result.exitCode == 0 && result.stdout == "42:resource:unloaded\ntwo words\n--flag\n" do
    throw (IO.userError s!"unexpected executable result: {result.stdout}")

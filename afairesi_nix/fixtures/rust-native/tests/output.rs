#[test]
fn executable_prints_42() {
    let output = std::process::Command::new(env!("CARGO_BIN_EXE_rust-native"))
        .output()
        .expect("run the built executable");
    assert!(output.status.success());
    assert_eq!(output.stdout, b"42\n");
}

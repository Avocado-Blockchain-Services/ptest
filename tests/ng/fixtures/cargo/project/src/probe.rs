pub fn exercise() {
    match std::env::var("PTEST_FIXTURE_MODE").as_deref() {
        Ok("fail") => panic!("fixture assertion failure"),
        Ok("exit") => std::process::exit(23),
        _ => {}
    }
    mark("start");
    std::thread::sleep(std::time::Duration::from_millis(5));
    mark("end");
}

fn mark(phase: &str) {
    use std::io::Write;
    let path = std::env::var("PTEST_FIXTURE_TRACE")
        .expect("Task11 must supply a run-owned trace path");
    let mut file = std::fs::OpenOptions::new().create(true).append(true).open(path).unwrap();
    let stamp = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap().as_nanos();
    writeln!(file, "{} {} {:?} {}", stamp, std::process::id(), std::thread::current().id(), phase).unwrap();
}

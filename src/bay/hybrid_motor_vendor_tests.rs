//! hybrid_motor.rs and model/ are vendored from GroundStation26 backend/src/apogee_planner/{hybrid.rs,model/}; the
//! copies must stay byte-identical so the planner and the plant fly the same motor. Skipped without a GroundStation26
//! checkout: `GS_REPO`, else `../GroundStation26` beside this repository.
use std::path::PathBuf;

const FILES: [(&str, &str); 6] = [
    ("src/bay/hybrid_motor.rs", "backend/src/apogee_planner/hybrid.rs"),
    ("src/bay/model/hybrid_motor.json", "backend/src/apogee_planner/model/hybrid_motor.json"),
    ("src/bay/model/sweet_v6_cea.json", "backend/src/apogee_planner/model/sweet_v6_cea.json"),
    ("src/bay/model/sweet_v6_grain.json", "backend/src/apogee_planner/model/sweet_v6_grain.json"),
    ("src/bay/model/hybrid_vectors.json", "backend/src/apogee_planner/model/hybrid_vectors.json"),
    ("src/bay/model/testfire_20261116.csv", "backend/src/apogee_planner/model/testfire_20261116.csv"),
];

fn gs_repo() -> Option<PathBuf> {
    let root = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    let gs = std::env::var_os("GS_REPO").map(PathBuf::from).unwrap_or_else(|| root.join("../GroundStation26"));
    gs.join(FILES[0].1).is_file().then_some(gs)
}

#[test]
fn vendored_hybrid_motor_matches_the_groundstation_copy() {
    let Some(gs) = gs_repo() else {
        eprintln!("skipped: no GroundStation26 checkout (set GS_REPO)");
        return;
    };
    let root = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    for (mine, theirs) in FILES {
        let a = std::fs::read(root.join(mine)).unwrap();
        let b = std::fs::read(gs.join(theirs)).unwrap_or_else(|e| panic!("{}: {e}", gs.join(theirs).display()));
        assert!(a == b, "{mine} differs from GroundStation26 {theirs}: copy it again");
    }
}

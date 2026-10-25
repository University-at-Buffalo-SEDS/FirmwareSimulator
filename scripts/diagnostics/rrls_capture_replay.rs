// Diagnostic-only overlay for the actual Mesh test module. No production API.
#[test]
fn replay_actual_radio_load() {
    let Some(input) = std::env::var_os("GS_CAPTURE_REPLAY_INPUT") else {
        assert!(std::env::var_os("GS_CAPTURE_REPLAY_OUTPUT").is_none());
        source_paced_campaign(true);
        return;
    };
    let output = std::env::var_os("GS_CAPTURE_REPLAY_OUTPUT").expect("explicit replay needs output");
    let step: u64 = std::env::var("GS_CAPTURE_REPLAY_STEP_MS").unwrap_or("10".into()).parse().unwrap();
    assert!((1..=10).contains(&step));
    let quota_bps: u64 = std::env::var("GS_CAPTURE_REPLAY_QUOTA_BPS").unwrap_or("16000".into()).parse().unwrap();
    assert!(matches!(quota_bps, 16_000 | 22_400));
    assert!(std::fs::metadata(&input).unwrap().len() <= 4*1024*1024);
    type Row = (u64, bool, Vec<u8>, Option<String>, Option<u64>);
    let rows: Vec<Row> = serde_json::from_slice(&std::fs::read(input).unwrap()).unwrap();
    assert!(!rows.is_empty() && rows.len() <= 10_000);
    crate::telemetry_schema::initialize().unwrap();
    let mut mesh = Mesh::new(Role::Rrls,2);
    let mut expected: Vec<Option<(u8, &[u8])>> = vec![None; rows.len()];
    let mut matched = vec![false;rows.len()];
    let mut deliveries = Vec::new();
    let mut index = 0;
    let mut next_egress = 0;
    let mut peak = 0;
    let mut expired_metadata = 0;
    let mut expired_ordinary = 0;
    let end = rows.last().unwrap().0/1000 + 3_000;
    for now in (0..=end+step).step_by(step as usize) {
        while index < rows.len() && rows[index].0/1000 <= now {
            let row = &rows[index];
            if mesh.active(now) {
                if !row.1 { expected[index]=Some((DOWN_DATA,&row.2)); }
                else if let Some((kind,body))=decode(&row.2) {
                    if kind==UP_DATA { expected[index]=Some((AV_BAY_DATA,body)); }
                }
            }
            mesh.incoming(row.1,&row.2,now);
            index+=1;
        }
        mesh.tick(now);
        peak=peak.max(mesh.queue.len());
        assert!(mesh.queue.len()<=MAX_FRAMES && mesh.bytes<=MAX_BYTES);
        if now < next_egress { continue; }
        let before_meta=mesh.queue.iter().filter(|item| item.independent_discovery).count();
        let before_ordinary=mesh.queue.iter().filter(|item| !item.independent_discovery && item.priority<253).count();
        let transmission=mesh.begin_transmission(now);
        let active_meta=transmission.as_ref().is_some_and(|item| item.queued.independent_discovery);
        let active_ordinary=transmission.as_ref().is_some_and(|item| !item.queued.independent_discovery && item.queued.priority<253);
        expired_metadata+=before_meta-mesh.queue.iter().filter(|item| item.independent_discovery).count()-usize::from(active_meta);
        expired_ordinary+=before_ordinary-mesh.queue.iter().filter(|item| !item.independent_discovery && item.priority<253).count()-usize::from(active_ordinary);
        let Some(transmission)=transmission else { continue; };
        let frame=transmission.frame();
        let cost=frame.bytes.len() as u64+4+u64::from(mesh.fair_overhead_bytes);
        next_egress=now+(cost*8*1000).div_ceil(quota_bps);
        let (kind,body)=decode(&frame.bytes).unwrap();
        if matches!(kind,DOWN_DATA|AV_BAY_DATA) {
            let found=expected.iter().enumerate().position(|(i,item)|
                !matched[i] && item.is_some_and(|(k,data)| k==kind && data==body));
            let i=found.expect("forwarded data must match a previously observed input exactly");
            matched[i]=true;
            deliveries.push(serde_json::json!({"index":i,"input_us":rows[i].0,
                "output_ms":now,"latency_ms":now as f64-rows[i].0 as f64/1000.0,
                "type":rows[i].3,"packet_id":rows[i].4}));
        }
        mesh.finish_transmission(transmission,true);
    }
    let missing: Vec<_>=expected.iter().enumerate().filter(|(i,item)| item.is_some()&&!matched[*i])
        .map(|(i,_)|serde_json::json!({"index":i,"input_us":rows[i].0,"type":rows[i].3,"packet_id":rows[i].4})).collect();
    let report=serde_json::json!({"clock_step_ms":step,"quota_bps":quota_bps,"input_frames":rows.len(),"peak_queue":peak,
        "expired_metadata":expired_metadata,"expired_ordinary":expired_ordinary,
        "aggregate_drops":mesh.dropped,"deliveries":deliveries,"missing":missing,
        "scope":"Actual Mesh replay with fixed captured input order/bytes, explicit16kbps or22.4kbps/16B quota (healthy25percent or active35percent at64kbps) and variable1..10ms callback cadence; no modem/OS scheduling claim. Outputs contain only input indices and decoded metadata, never raw payloads."});
    std::fs::write(output,serde_json::to_vec_pretty(&report).unwrap()).unwrap();
}

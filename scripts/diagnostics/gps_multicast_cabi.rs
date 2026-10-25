// Diagnostic-only actual C ABI publication fixture, inserted in a private SDK clone.
#[cfg(test)]
mod gps_multicast_diagnostic {
    use super::*;
    use std::sync::Mutex;
    #[test]
    fn gps_three_endpoints_reach_independent_can_and_radio_segments() {
        crate::config::register_schema_json_file_with_budget(
            &std::env::var("SEDSNET_GPS_DIAGNOSTIC_SCHEMA").unwrap(), 2*1024*1024).unwrap();
        let gps = DataType::named("GPS_DATA");
        let ground=DataEndpoint::named("GROUND_STATION");
        let card=DataEndpoint::named("SD_CARD");
        let fc=DataEndpoint::named("FLIGHT_CONTROLLER");
        let seen=Arc::new(Mutex::new([0usize;2]));
        let router=Arc::new(Router::new(RouterConfig::new([]).with_sender("RF")));
        for (index,name,peer,endpoints) in [(0,"can","FC",vec![card,fc]),(1,"radio","GS",vec![ground])] {
            let sink=seen.clone();
            let side=router.add_side_packed(name,move |bytes| {
                if crate::wire_format::peek_frame_info(bytes).is_ok_and(|frame| frame.envelope.ty==gps) {
                    sink.lock().unwrap()[index]+=1;
                }
                Ok(())
            });
            router.rx_from_side(&crate::discovery::build_discovery_announce(peer,1,&endpoints).unwrap(),side).unwrap();
        }
        router.process_all_queues_with_timeout(0).unwrap();
        *seen.lock().unwrap()=[0,0];
        let mut handle=SedsRouter {inner:router};
        let coordinates=[43.0f32,-78.0,150.0];
        assert_eq!(seds_router_log_typed(&mut handle,gps.as_u32(),coordinates.as_ptr().cast(),3,4,2),0);
        handle.inner.process_all_queues_with_timeout(0).unwrap();
        let counts=*seen.lock().unwrap();
        println!("GPS_MULTICAST_COUNTS can={} radio={}",counts[0],counts[1]);
        assert_eq!(counts,[1,1],"independent endpoint segments must each receive GPS exactly once");
    }
}

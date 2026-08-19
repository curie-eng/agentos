use std::cell::Cell;
use std::collections::BTreeSet;
use std::net::IpAddr;

use curie::credcheck::check_model_credential;
use curie::exit::ExitClass;
use curie::ops::{parse_egress_provider, provider_egress_hosts, resolve_provider_egress_cidrs};
use serde::Deserialize;

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct ProviderRow {
    name: String,
    base_url: Option<String>,
    egress_hosts: Vec<String>,
    credential_examples: Vec<String>,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct ProviderRegistry {
    providers: Vec<ProviderRow>,
    unknown_provider_names: Vec<String>,
    rejected_credential_examples: Vec<String>,
}

fn registry_source() -> &'static str {
    include_str!(concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/../tests/vectors/model-provider-registry.json"
    ))
}

fn parse_registry(raw: &str) -> Result<ProviderRegistry, String> {
    let registry: ProviderRegistry =
        serde_json::from_str(raw).map_err(|error| error.to_string())?;
    if registry.providers.is_empty() {
        return Err("provider registry is empty".to_string());
    }
    Ok(registry)
}

fn load_registry() -> ProviderRegistry {
    parse_registry(registry_source()).expect("parse model provider registry")
}

#[test]
fn every_supported_provider_passes_credential_and_egress_checks() {
    let registry = load_registry();
    assert!(!registry.providers.is_empty(), "no providers parsed");

    let names = registry
        .providers
        .iter()
        .map(|provider| provider.name.as_str())
        .collect::<BTreeSet<_>>();
    assert_eq!(names.len(), registry.providers.len(), "duplicate provider");

    let expected_names = registry
        .providers
        .iter()
        .map(|provider| provider.name.as_str())
        .collect::<Vec<_>>()
        .join(", ");
    let error = parse_egress_provider("registry-probe").expect_err("probe must be unknown");
    assert_eq!(
        error.message,
        format!(
            "`--allow-egress-host` value `registry-probe` is not a known provider (expected one of: {expected_names})"
        )
    );

    for provider in &registry.providers {
        let endpoint = provider.base_url.as_deref().unwrap_or("SDK default");
        assert!(
            !provider.credential_examples.is_empty(),
            "{} at {endpoint} has no credential examples",
            provider.name
        );
        for credential in &provider.credential_examples {
            check_model_credential(credential).unwrap_or_else(|error| {
                panic!(
                    "{} credential for {endpoint} was rejected: {error}",
                    provider.name
                )
            });
        }

        assert_eq!(
            parse_egress_provider(&provider.name).expect("supported provider must parse"),
            provider.name.as_str()
        );
        let actual_hosts = provider_egress_hosts(&provider.name)
            .expect("supported provider must have egress hosts");
        let expected_hosts = provider
            .egress_hosts
            .iter()
            .map(String::as_str)
            .collect::<Vec<_>>();
        assert_eq!(
            actual_hosts, expected_hosts,
            "{} egress hosts",
            provider.name
        );
    }
}

#[test]
fn unknown_providers_fail_before_dns_resolution() {
    let registry = load_registry();
    assert!(!registry.unknown_provider_names.is_empty());

    for unknown in &registry.unknown_provider_names {
        assert!(provider_egress_hosts(unknown).is_none(), "{unknown:?}");
        assert_eq!(
            parse_egress_provider(unknown)
                .expect_err("unknown provider must fail")
                .class,
            ExitClass::Usage,
            "{unknown:?}"
        );

        let resolver_called = Cell::new(false);
        let input = vec![unknown.clone()];
        let result = resolve_provider_egress_cidrs(&input, |_host| {
            resolver_called.set(true);
            Ok(vec!["1.1.1.1".parse::<IpAddr>().expect("public address")])
        });
        assert!(result.is_err(), "{unknown:?}");
        assert!(!resolver_called.get(), "DNS called for {unknown:?}");
    }
}

#[test]
fn rejected_credential_examples_stay_rejected() {
    let registry = load_registry();
    assert!(!registry.rejected_credential_examples.is_empty());
    for credential in &registry.rejected_credential_examples {
        let error = match check_model_credential(credential) {
            Ok(()) => panic!("accepted rejected credential {credential:?}"),
            Err(error) => error,
        };
        for recovery in [
            "CURIE_MODEL_BASE_URL",
            "curie secrets set <NAME>",
            "export <NAME>=",
        ] {
            assert!(
                error.contains(recovery),
                "credential error must name recovery {recovery:?}: {error}"
            );
        }
    }
}

//! Integration: the missing-release recovery command uses the provider inferred
//! by the same credential-prefix map as `curie cluster up`.

use curie::doctor::{evaluate, Facts};
use curie::ops::provider_from_credential_prefix;

fn missing_release_recovery(credential: Option<&str>) -> String {
    let facts = Facts {
        model_credential: credential.map(|_| "CURIE_CREDENTIALS".into()),
        model_credential_source: credential.map(|_| "environment".into()),
        model_credential_provider: credential.and_then(provider_from_credential_prefix),
        kube_context: Some("minikube".into()),
        ..Default::default()
    };

    evaluate(&facts)
        .into_iter()
        .find(|check| check.id == "release")
        .and_then(|check| check.fix)
        .expect("a missing release must offer a recovery command")
}

#[test]
fn missing_release_recovery_infers_egress_from_credential_prefix() {
    let openrouter = missing_release_recovery(Some("sk-or-PLACEHOLDER"));
    assert!(
        openrouter.starts_with("curie cluster up --namespace <ns> --release <name>"),
        "recovery command must be runnable: {openrouter}"
    );
    assert!(
        openrouter.contains("--allow-egress-host openrouter"),
        "OpenRouter credential must select OpenRouter egress: {openrouter}"
    );
    assert!(
        !openrouter.contains("--allow-egress-host anthropic"),
        "OpenRouter credential must not select Anthropic egress: {openrouter}"
    );

    let anthropic = missing_release_recovery(Some("sk-ant-PLACEHOLDER"));
    assert!(
        anthropic.contains("--allow-egress-host anthropic"),
        "Anthropic credential must select Anthropic egress: {anthropic}"
    );

    for credential in [None, Some("unknown-PLACEHOLDER")] {
        assert_eq!(
            missing_release_recovery(credential),
            "curie cluster up --namespace <ns> --release <name>",
            "no unambiguous provider means egress remains sealed"
        );
    }
}

//! Private local secret storage for Curie workflows.
//!
//! Secret values live in a mode-0600 file under the Curie config directory,
//! avoiding repeated platform credential-store authorization dialogs. `keyring`
//! remains only as a read-only migration path for older Curie installations.

use std::collections::{BTreeMap, BTreeSet};
use std::fs;
use std::io::{self, IsTerminal, Write};
use std::path::{Path, PathBuf};
use std::sync::{Mutex, OnceLock};

use anyhow::{bail, Context, Result};
use serde::{Deserialize, Serialize};

const SERVICE: &str = "ai.curietech.curie";
const VAULT_ACCOUNT: &str = "curie:global:vault";

#[derive(Clone, Debug)]
pub struct SetSecretOpts {
    pub name: String,
    pub from_env: Option<String>,
}

#[derive(Clone, Debug)]
pub struct UnsetSecretOpts {
    pub name: String,
}

#[derive(Clone, Debug, Serialize, Deserialize, Default)]
struct SecretIndex {
    #[serde(default)]
    vault: bool,
    #[serde(default)]
    legacy_names: BTreeSet<String>,
    names: BTreeSet<String>,
    #[serde(default)]
    file_names: BTreeSet<String>,
}

#[derive(Clone, Debug, Serialize, Deserialize, Default)]
struct SecretVault {
    values: BTreeMap<String, String>,
}

#[derive(Default)]
struct VaultCache {
    loaded: bool,
    vault: SecretVault,
}

static VAULT_CACHE: OnceLock<Mutex<VaultCache>> = OnceLock::new();

pub fn set(opts: SetSecretOpts) -> Result<()> {
    validate_name(&opts.name)?;
    let value = match opts.from_env {
        Some(var) => std::env::var(&var)
            .with_context(|| format!("{var} is not set; cannot save {}", opts.name))?,
        None => prompt_secret(&opts.name)?,
    };
    save_value(&opts.name, &value)?;
    crate::ui::ui().success(&format!("saved {} in Curie private storage", opts.name));
    Ok(())
}

pub fn list() -> Result<()> {
    crate::ui::ui().emit(&SecretsListOutput {
        names: list_names()?,
    });
    Ok(())
}

/// Output of `secrets list` (#474): the saved secret NAMEs. Routes through the
/// one `Ui::emit` point rather than an inline `if json()` branch. Public so the
/// schema contract test (#634) can build one and validate `to_json` against
/// `cli/schema/secrets.schema.json`.
pub struct SecretsListOutput {
    pub names: Vec<String>,
}

impl crate::ui::CliOutput for SecretsListOutput {
    fn to_json(&self) -> serde_json::Value {
        serde_json::json!({ "secrets": self.names })
    }

    fn render(&self, ui: &crate::ui::Ui) {
        if self.names.is_empty() {
            ui.note("no Curie secrets saved");
        } else {
            ui.payload_plain(&self.names.join("\n"));
        }
    }
}

pub fn unset(opts: UnsetSecretOpts) -> Result<()> {
    remove_value(&opts.name)?;
    crate::ui::ui().success(&format!("removed {}", opts.name));
    Ok(())
}

pub fn get_value(name: &str) -> Result<Option<String>> {
    validate_name(name)?;
    if let Some(value) = read_credentials()?.values.get(name).cloned() {
        return Ok(Some(value));
    }
    sync_secret_file()?;
    if let Some(value) = read_credentials()?.values.get(name).cloned() {
        return Ok(Some(value));
    }
    if needs_vault_upgrade(name)? {
        migrate_legacy_value(name)?;
    }
    Ok(read_credentials()?.values.get(name).cloned())
}

pub(crate) fn resolve_env_or_saved(name: &str) -> Result<Option<String>> {
    std::env::var(name)
        .ok()
        .filter(|value| !value.is_empty())
        .map(Ok)
        .or_else(|| get_value(name).transpose())
        .transpose()
}

/// Check the non-secret index without opening any credential values.
/// UI status rendering must use this instead of `get_value`.
pub fn is_saved(name: &str) -> Result<bool> {
    validate_name(name)?;
    let index = read_index()?;
    Ok(index.file_names.contains(name)
        || index.names.contains(name)
        || index.legacy_names.contains(name))
}

/// Whether a name belongs to the older one-Keychain-item-per-secret layout.
/// This reads only the non-secret index and never opens Keychain.
pub fn needs_vault_upgrade(name: &str) -> Result<bool> {
    validate_name(name)?;
    let index = read_index()?;
    Ok(if index.vault {
        index.legacy_names.contains(name)
    } else {
        index.names.contains(name)
    })
}

/// Reconcile an older non-secret index with the consolidated vault. This opens
/// exactly one credential-store item and never reads legacy per-secret items.
pub fn sync_secret_file() -> Result<()> {
    let credentials = read_credentials()?;
    if !credentials.values.is_empty() {
        let index = reconcile_file_names(read_index()?, credentials.values.keys());
        return write_index(&index);
    }
    let index = read_index()?;
    if index.names.is_empty() {
        return Ok(());
    }
    let vault = read_vault()?;
    if vault.values.is_empty() {
        return Ok(());
    }
    write_credentials(&vault)?;
    let index = reconcile_file_names(index, vault.values.keys());
    write_index(&index)
}

/// Copy one required credential from the legacy per-secret Keychain layout to
/// the private file. The old Keychain item is deliberately left untouched.
pub fn migrate_legacy_value(name: &str) -> Result<bool> {
    if !needs_vault_upgrade(name)? {
        return Ok(false);
    }
    let value = match legacy_entry(name)?.get_password() {
        Ok(value) => value,
        Err(keyring::Error::NoEntry) => return Ok(false),
        Err(err) => {
            return Err(err)
                .with_context(|| format!("authorizing saved credential {name} for migration"));
        }
    };
    save_value(name, &value)?;
    Ok(true)
}

pub fn set_value(name: &str, value: &str) -> Result<()> {
    validate_name(name)?;
    let mut credentials = read_credentials()?;
    credentials
        .values
        .insert(name.to_string(), value.to_string());
    write_credentials(&credentials)
}

pub fn save_value(name: &str, value: &str) -> Result<()> {
    validate_name(name)?;
    if value.is_empty() {
        bail!("refusing to store an empty secret for {name}");
    }
    set_value(name, value)?;
    add_to_index(name)
}

pub fn delete_value(name: &str) -> Result<()> {
    validate_name(name)?;
    let mut credentials = read_credentials()?;
    if credentials.values.remove(name).is_some() {
        write_credentials(&credentials)?;
    }
    Ok(())
}

pub fn remove_value(name: &str) -> Result<()> {
    validate_name(name)?;
    delete_value(name)?;
    remove_from_index(name)
}

pub fn list_names() -> Result<Vec<String>> {
    let index = read_index()?;
    Ok(index
        .names
        .into_iter()
        .chain(index.legacy_names)
        .chain(index.file_names)
        .collect::<BTreeSet<_>>()
        .into_iter()
        .collect())
}

pub fn validate_name(name: &str) -> Result<()> {
    if name.is_empty() {
        bail!("secret name is required");
    }
    let valid = name
        .chars()
        .all(|c| c.is_ascii_uppercase() || c.is_ascii_digit() || c == '_')
        && name
            .chars()
            .next()
            .is_some_and(|c| c.is_ascii_uppercase() || c == '_');
    if !valid {
        bail!(
            "secret name must look like an environment variable, e.g. GITHUB_PERSONAL_ACCESS_TOKEN"
        );
    }
    Ok(())
}

fn prompt_secret(name: &str) -> Result<String> {
    if !io::stdin().is_terminal() || !io::stdout().is_terminal() {
        bail!("setting {name} requires a terminal; use --from-env VAR in non-interactive contexts");
    }
    print!("{name}: ");
    io::stdout().flush().ok();
    rpassword::read_password().context("reading secret from terminal")
}

fn vault_entry() -> Result<keyring::Entry> {
    keyring::Entry::new(SERVICE, VAULT_ACCOUNT)
        .context("opening the Curie OS credential-store vault")
}

fn legacy_entry(name: &str) -> Result<keyring::Entry> {
    keyring::Entry::new(SERVICE, &format!("curie:global:{name}"))
        .with_context(|| format!("opening legacy OS credential-store entry for {name}"))
}

fn vault_cache() -> &'static Mutex<VaultCache> {
    VAULT_CACHE.get_or_init(|| Mutex::new(VaultCache::default()))
}

fn read_vault() -> Result<SecretVault> {
    let mut cache = vault_cache()
        .lock()
        .map_err(|_| anyhow::anyhow!("Curie credential vault cache is unavailable"))?;
    if cache.loaded {
        return Ok(cache.vault.clone());
    }
    let vault = match vault_entry()?.get_password() {
        Ok(raw) => serde_json::from_str(&raw).context("parsing the Curie credential vault")?,
        Err(keyring::Error::NoEntry) => SecretVault::default(),
        Err(err) => return Err(err).context("reading the Curie OS credential-store vault"),
    };
    cache.loaded = true;
    cache.vault = vault.clone();
    Ok(vault)
}

fn read_credentials() -> Result<SecretVault> {
    let path = credentials_path()?;
    if !path.is_file() {
        return Ok(SecretVault::default());
    }
    let raw = fs::read_to_string(&path)
        .with_context(|| format!("reading Curie credentials {}", path.display()))?;
    serde_json::from_str(&raw)
        .with_context(|| format!("parsing Curie credentials {}", path.display()))
}

fn write_credentials(credentials: &SecretVault) -> Result<()> {
    let path = credentials_path()?;
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)
            .with_context(|| format!("creating Curie config dir {}", parent.display()))?;
    }
    let body = serde_json::to_vec_pretty(credentials).context("serializing Curie credentials")?;
    write_private(&path, &body)
}

fn add_to_index(name: &str) -> Result<()> {
    let index = mark_file_saved(read_index()?, name);
    write_index(&index)
}

fn mark_file_saved(mut index: SecretIndex, name: &str) -> SecretIndex {
    index.legacy_names.remove(name);
    index.names.remove(name);
    index.file_names.insert(name.to_string());
    index
}

fn reconcile_file_names<'a>(
    mut index: SecretIndex,
    file_names: impl Iterator<Item = &'a String>,
) -> SecretIndex {
    for name in file_names {
        index.legacy_names.remove(name);
        index.names.remove(name);
        index.file_names.insert(name.clone());
    }
    index
}

fn remove_from_index(name: &str) -> Result<()> {
    let mut index = read_index()?;
    index.names.remove(name);
    index.legacy_names.remove(name);
    index.file_names.remove(name);
    write_index(&index)
}

fn read_index() -> Result<SecretIndex> {
    let path = index_path()?;
    if !path.is_file() {
        return Ok(SecretIndex::default());
    }
    let raw = fs::read_to_string(&path)
        .with_context(|| format!("reading secret index {}", path.display()))?;
    serde_json::from_str(&raw).with_context(|| format!("parsing secret index {}", path.display()))
}

fn write_index(index: &SecretIndex) -> Result<()> {
    let path = index_path()?;
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)
            .with_context(|| format!("creating Curie config dir {}", parent.display()))?;
    }
    let body = serde_json::to_vec_pretty(index).context("serializing secret index")?;
    write_private(&path, &body)
}

#[cfg(unix)]
fn write_private(path: &Path, body: &[u8]) -> Result<()> {
    use std::os::unix::fs::{OpenOptionsExt, PermissionsExt};

    let mut file = fs::OpenOptions::new()
        .create(true)
        .truncate(true)
        .write(true)
        .mode(0o600)
        .open(path)
        .with_context(|| format!("opening private file {}", path.display()))?;
    file.set_permissions(fs::Permissions::from_mode(0o600))
        .with_context(|| format!("securing private file {}", path.display()))?;
    file.write_all(body)
        .with_context(|| format!("writing private file {}", path.display()))
}

#[cfg(not(unix))]
fn write_private(path: &Path, body: &[u8]) -> Result<()> {
    fs::write(path, body).with_context(|| format!("writing private file {}", path.display()))
}

fn index_path() -> Result<PathBuf> {
    Ok(config_dir()?.join("secrets.json"))
}

fn credentials_path() -> Result<PathBuf> {
    Ok(config_dir()?.join("credentials.json"))
}

fn config_dir() -> Result<PathBuf> {
    if let Ok(dir) = std::env::var("CURIE_CONFIG_DIR") {
        return Ok(PathBuf::from(dir));
    }
    let home = std::env::var("HOME").context("HOME is not set; cannot locate Curie config dir")?;
    Ok(PathBuf::from(home).join(".config/curie"))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn validates_env_like_names() {
        assert!(validate_name("GITHUB_PERSONAL_ACCESS_TOKEN").is_ok());
        assert!(validate_name("_TOKEN1").is_ok());
        assert!(validate_name("github_token").is_err());
        assert!(validate_name("1TOKEN").is_err());
        assert!(validate_name("TOKEN-NOPE").is_err());
    }

    #[test]
    fn legacy_index_does_not_claim_per_key_secrets_are_in_the_vault() {
        let index: SecretIndex = serde_json::from_str(
            r#"{"names":["ANTHROPIC_API_KEY","GITHUB_PERSONAL_ACCESS_TOKEN"]}"#,
        )
        .unwrap();

        assert!(!index.vault);
        assert_eq!(index.names.len(), 2);
    }

    #[test]
    fn upgrading_one_legacy_name_preserves_every_other_name() {
        let legacy: SecretIndex = serde_json::from_str(
            r#"{"names":["ANTHROPIC_API_KEY","GITHUB_PERSONAL_ACCESS_TOKEN","OPENAI_API_KEY"]}"#,
        )
        .unwrap();

        let upgraded = mark_file_saved(legacy, "ANTHROPIC_API_KEY");

        assert_eq!(
            upgraded.names,
            BTreeSet::from([
                "GITHUB_PERSONAL_ACCESS_TOKEN".to_string(),
                "OPENAI_API_KEY".to_string()
            ])
        );
        assert_eq!(
            upgraded.file_names,
            BTreeSet::from(["ANTHROPIC_API_KEY".to_string()])
        );
    }

    #[test]
    fn consolidated_vault_names_recover_a_stale_legacy_index() {
        let legacy: SecretIndex = serde_json::from_str(
            r#"{"names":["ANTHROPIC_API_KEY","GITHUB_PERSONAL_ACCESS_TOKEN","OPENAI_API_KEY"]}"#,
        )
        .unwrap();
        let file_names = [
            "ANTHROPIC_API_KEY".to_string(),
            "GITHUB_PERSONAL_ACCESS_TOKEN".to_string(),
        ];

        let recovered = reconcile_file_names(legacy, file_names.iter());

        assert_eq!(
            recovered.names,
            BTreeSet::from(["OPENAI_API_KEY".to_string()])
        );
        assert_eq!(recovered.file_names, BTreeSet::from(file_names));
    }

    #[test]
    fn vault_round_trips_multiple_credentials_in_one_payload() {
        let vault = SecretVault {
            values: BTreeMap::from([
                ("ANTHROPIC_API_KEY".to_string(), "model-secret".to_string()),
                (
                    "GITHUB_PERSONAL_ACCESS_TOKEN".to_string(),
                    "github-secret".to_string(),
                ),
            ]),
        };
        let raw = serde_json::to_string(&vault).unwrap();
        let decoded: SecretVault = serde_json::from_str(&raw).unwrap();

        assert_eq!(decoded.values, vault.values);
        assert_eq!(VAULT_ACCOUNT, "curie:global:vault");
    }

    #[cfg(unix)]
    #[test]
    fn private_files_are_forced_to_owner_only_permissions() {
        use std::os::unix::fs::PermissionsExt;

        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("credentials.json");
        fs::write(&path, b"old").unwrap();
        fs::set_permissions(&path, fs::Permissions::from_mode(0o644)).unwrap();

        write_private(&path, br#"{"values":{}}"#).unwrap();

        assert_eq!(
            fs::metadata(path).unwrap().permissions().mode() & 0o777,
            0o600
        );
    }
}

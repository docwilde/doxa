#![cfg(unix)]
use std::{fs, path::{Path,PathBuf}, process::{Command,Output}};
use std::os::unix::fs::{symlink,PermissionsExt};
use serde_json::json;

struct Fixture { _root:tempfile::TempDir, home:PathBuf, claude:PathBuf, plugin:PathBuf }
impl Fixture {
    fn new()->Self {
        let root=tempfile::tempdir().unwrap();
        let home=root.path().join("doxa");let claude=root.path().join("claude");let plugin=root.path().join("native-plugin");
        for path in [&home,&claude,&plugin] { fs::create_dir_all(path).unwrap();fs::set_permissions(path,fs::Permissions::from_mode(0o700)).unwrap(); }
        for name in ["commands","hooks",".claude-plugin"] {fs::create_dir_all(plugin.join(name)).unwrap();}
        fs::write(plugin.join("commands/fixture.md"),"---\ndescription: Native fixture command\n---\nprivate credential sentinel\n").unwrap();
        fs::write(plugin.join("hooks/hooks.json"),"private hook sentinel").unwrap();
        fs::write(plugin.join(".mcp.json"),"private MCP sentinel").unwrap();
        fs::write(plugin.join(".claude-plugin/plugin.json"),json!({"name":"fixture","hooks":"private hook sentinel","mcpServers":"private MCP sentinel","lspServers":"private LSP sentinel"}).to_string()).unwrap();
        fs::write(claude.join("settings.json"),json!({"enabledPlugins":{"fixture@local":true,"lore@lore":true},"userSetting":"private settings sentinel"}).to_string()).unwrap();
        fs::create_dir_all(claude.join("plugins")).unwrap();
        fs::write(claude.join("plugins/installed_plugins.json"),json!({"version":2,"plugins":{"fixture@local":[{"installPath":plugin}],"lore@lore":[{"installPath":plugin}]}}).to_string()).unwrap();
        Self{_root:root,home,claude,plugin}
    }
    fn run(&self,args:&[&str],override_value:Option<&str>)->Output {
        let mut command=Command::new(env!("CARGO_BIN_EXE_doxa-rs"));
        command.env_clear().args(args).env("PATH","/usr/bin:/bin").env("HOME",self._root.path().join("user"))
            .env("DOXA_HOME",&self.home).env("CLAUDE_CONFIG_DIR",&self.claude);
        if let Some(value)=override_value {command.env("DOXA_ADOPT_PLUGINS",value);}
        command.output().unwrap()
    }
    fn adopted(&self)->PathBuf {self.home.join("claude-cli/adopted-plugins/fixture@local")}
}
fn private_output_withheld(output:&Output) {
    assert!(!String::from_utf8_lossy(&output.stdout).contains("sentinel"));
    assert!(!String::from_utf8_lossy(&output.stderr).contains("sentinel"));
}
fn policy(home:&Path)->bool {
    fs::read_to_string(home.join("config.toml")).unwrap().parse::<toml::Value>().unwrap()["adopt_plugins"].as_bool().unwrap()
}

#[test]
fn plugins_cli_updates_only_doxa_policy_and_respects_environment() {
    let fixture=Fixture::new();
    let settings=fs::read(fixture.claude.join("settings.json")).unwrap();
    let installed=fs::read(fixture.claude.join("plugins/installed_plugins.json")).unwrap();
    let manifest=fs::read(fixture.plugin.join(".claude-plugin/plugin.json")).unwrap();
    assert!(fixture.run(&["plugins","adopt","on"],None).status.success());
    assert!(policy(&fixture.home));
    assert!(!fixture.adopted().exists(),"policy change alone must not stage source artifacts");
    let refresh=fixture.run(&["plugins","refresh"],None);assert!(refresh.status.success());private_output_withheld(&refresh);
    let report=String::from_utf8_lossy(&refresh.stdout);
    assert!(report.contains("adoption: ON"));assert!(report.contains("/fixture:fixture"));
    assert!(report.contains("duplicate carrier blocked"));
    let adopted=fixture.adopted();
    assert_eq!(fs::read(adopted.join("commands/fixture.md")).unwrap(),fs::read(fixture.plugin.join("commands/fixture.md")).unwrap());
    assert!(!adopted.join("hooks").exists());assert!(!adopted.join(".mcp.json").exists());
    let sanitized:serde_json::Value=serde_json::from_slice(&fs::read(adopted.join(".claude-plugin/plugin.json")).unwrap()).unwrap();
    for key in ["hooks","mcpServers","lspServers"] {assert!(sanitized.get(key).is_none());}
    assert!(!fixture.home.join("claude-cli/adopted-plugins/lore@lore").exists());
    assert!(!fixture.run(&["plugins","adopt","off"],Some("1")).status.success());assert!(policy(&fixture.home));
    assert!(fixture.run(&["plugins","adopt","off"],None).status.success());assert!(!policy(&fixture.home));
    fs::remove_dir_all(adopted).unwrap();
    let off=fixture.run(&["plugins","refresh"],Some("0"));assert!(off.status.success());assert!(!fixture.adopted().exists());
    assert!(String::from_utf8_lossy(&off.stdout).contains("adoption: OFF"));
    let on=fixture.run(&["plugins","refresh"],Some("1"));assert!(on.status.success());assert!(fixture.adopted().exists());assert!(!policy(&fixture.home));private_output_withheld(&on);
    assert_eq!(fs::read(fixture.claude.join("settings.json")).unwrap(),settings);
    assert_eq!(fs::read(fixture.claude.join("plugins/installed_plugins.json")).unwrap(),installed);
    assert_eq!(fs::read(fixture.plugin.join(".claude-plugin/plugin.json")).unwrap(),manifest);
    assert!(!fixture.run(&["auth","login"],None).status.success());
    assert!(!fixture.run(&["auth","logout","inferred"],None).status.success());
}

#[test]
fn plugin_refresh_refuses_unsafe_malformed_or_oversized_native_sources() {
    for case in ["malformed-installed","malformed-settings","oversized-installed","oversized-settings","symlink-installed","hardlink-settings"] {
        let fixture=Fixture::new();
        let installed=fixture.claude.join("plugins/installed_plugins.json");let settings=fixture.claude.join("settings.json");
        match case {
            "malformed-installed"=>fs::write(&installed,"private credential sentinel").unwrap(),
            "malformed-settings"=>fs::write(&settings,"private settings sentinel").unwrap(),
            "oversized-installed"=>fs::write(&installed,format!("private inventory sentinel{}","x".repeat(1024*1024))).unwrap(),
            "oversized-settings"=>fs::write(&settings,format!("private settings sentinel{}","x".repeat(65536))).unwrap(),
            "symlink-installed"=>{let source=fixture._root.path().join("private-source");fs::rename(&installed,&source).unwrap();symlink(source,&installed).unwrap();},
            "hardlink-settings"=>fs::hard_link(&settings,fixture._root.path().join("private-settings-link")).unwrap(),
            _=>unreachable!(),
        }
        let original_installed=fs::read(&installed).unwrap();let original_settings=fs::read(&settings).unwrap();
        let refused=fixture.run(&["plugins","refresh"],Some("1"));
        assert!(!refused.status.success(),"{case}: {refused:?}");private_output_withheld(&refused);
        assert!(refused.stdout.len()<1024 && refused.stderr.len()<1024,"{case} exposed unbounded private input");
        assert!(!fixture.adopted().exists());assert!(!fixture.home.join("config.toml").exists());
        assert_eq!(fs::read(installed).unwrap(),original_installed);assert_eq!(fs::read(settings).unwrap(),original_settings);
    }
}

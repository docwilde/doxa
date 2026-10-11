use super::{App, mpsc, Receiver, TryRecvError};
use crate::router_setup::{Availability, Review};
use std::path::{Path, PathBuf};

#[derive(Debug)]
pub(super) struct RouterReview {
    pub path: PathBuf,
    pub state: ReviewState,
}

#[derive(Debug)]
pub(super) enum ReviewState {
    Running(Receiver<Result<Review, &'static str>>),
    Complete(Result<Review, &'static str>),
}

impl App {
    pub(super) fn start_router_review(&mut self, path: PathBuf) -> bool {
        if self.router_review.as_ref().is_some_and(|review| matches!(review.state, ReviewState::Running(_))) {
            self.notice="Jev Router account/catalog review is already running".into();
            return false;
        }
        if path.as_os_str().is_empty() || !path.is_absolute() {
            self.notice="Set Jev Router config to an absolute owner-private JSON path, then Ctrl+P reviews it".into();
            return false;
        }
        let Ok(cwd)=std::env::current_dir() else {return false;};
        if cfg!(test) { return false; }
        let (tx,rx)=mpsc::sync_channel(1);
        let worker_path=path.clone();
        if std::thread::Builder::new().name("router-settings-review".into()).spawn(move || {
            let _=tx.send(crate::router_setup::review(&worker_path,&cwd));
        }).is_err() {self.notice="Router review worker unavailable; retry Ctrl+P".into();return false;}
        self.router_review=Some(RouterReview{path,state:ReviewState::Running(rx)});
        self.notice="Checking Router config and current account catalogs; no Jev or worker turn".into();
        true
    }

    pub(super) fn router_review_result(&self, path: &Path) -> Option<&Review> {
        self.router_review.as_ref().filter(|review|review.path==path).and_then(|review|match &review.state {
            ReviewState::Complete(Ok(result))=>Some(result), _=>None,
        })
    }

    pub(super) fn router_review_lines(&self, path: &Path) -> Vec<String> {
        let Some(review)=self.router_review.as_ref().filter(|review|review.path==path) else {
            return vec!["Not checked · Ctrl+P reviews config/auth/catalog; no inference".into()];
        };
        match &review.state {
            ReviewState::Running(_)=>vec!["Checking current account catalogs… no Jev or worker turn".into()],
            ReviewState::Complete(Err(reason))=>vec![(*reason).into()],
            ReviewState::Complete(Ok(result)) if !result.current(path)=>vec!["Review expired or config/credential changed · Ctrl+P refreshes".into()],
            ReviewState::Complete(Ok(result))=> {
                let mut lines=vec![result.summary(),result.cost_scope().into()];
                lines.extend(result.targets.iter().map(|target|target.label()));
                lines
            }
        }
    }

    pub(super) fn poll_router_review(&mut self) -> bool {
        let Some(review)=self.router_review.as_mut() else {return false;};
        let ReviewState::Running(rx)=&review.state else {return false;};
        let result=match rx.try_recv() {
            Ok(result)=>result,
            Err(TryRecvError::Disconnected)=>Err("Account/catalog review worker stopped; retry"),
            Err(TryRecvError::Empty)=>return false,
        };
        self.notice=match &result {
            Ok(result)=>format!("Jev Router: {} · Ctrl+S saves staged settings",result.summary()),
            Err(reason)=>format!("Jev Router review: {reason}"),
        };
        if let Some(form)=self.new_session.as_mut().filter(|form|
            form.engine==crate::launch::Engine::Router && form.router_config.as_deref()==Some(review.path.as_path())) {
            form.catalog_pending=false;
            form.models=vec!["auto".into()];
            match &result {
                Ok(result)=> {
                    form.models.extend(result.targets.iter().filter(|target|target.availability==Availability::Available)
                        .map(|target|target.id.clone()));
                    form.catalog_note=format!("{} · {}",result.summary(),result.cost_scope());
                }
                Err(reason)=>{form.catalog_note=format!("{reason} · R retries");},
            }
        }
        review.state=ReviewState::Complete(result);
        self.refresh_choice_dropdown();
        true
    }

    pub(super) fn router_target_label(&self, id: &str) -> String {
        if id=="auto" {return "Auto · Jev chooses once at each turn boundary".into();}
        self.new_session.as_ref().and_then(|form|form.router_config.as_deref())
            .and_then(|path|self.router_review_result(path))
            .and_then(|review|review.targets.iter().find(|target|target.id==id))
            .map(|target|target.label()).unwrap_or_else(||format!("{id} · account/catalog unverified"))
    }
}

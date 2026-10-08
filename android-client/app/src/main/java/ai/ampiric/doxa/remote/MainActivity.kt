package ai.ampiric.doxa.remote

import android.Manifest
import android.content.pm.PackageManager
import android.content.SharedPreferences
import android.os.Build
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.LazyRow
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import kotlinx.coroutines.*
import org.json.JSONArray
import org.json.JSONObject
import java.io.ByteArrayOutputStream

private data class Entry(val kind: String, val text: String)
private class ReplayGap : Exception()

private class RemoteController(private val prefs: SharedPreferences, private val scope: CoroutineScope,
                               private val alerts: LocalAlerts) {
    var origin by mutableStateOf(prefs.getString("hub", "") ?: "")
    var status by mutableStateOf("Connect through your user-owned Tailscale device")
        private set
    var connected by mutableStateOf(false)
        private set
    var keySelected by mutableStateOf(false)
        private set
    var sessions by mutableStateOf<List<Session>>(emptyList())
        private set
    var selected by mutableStateOf<Session?>(null)
        private set
    var entries by mutableStateOf<List<Entry>>(emptyList())
        private set
    var pending by mutableStateOf<List<JSONObject>>(emptyList())
        private set
    var olderBefore by mutableStateOf<Long?>(null)
        private set
    var draft by mutableStateOf(prefs.getString("draft", "") ?: "")
        private set
    var busy by mutableStateOf(false)
        private set
    var uncertain by mutableStateOf<PendingCommand?>(null)
        private set
    var confirmFresh by mutableStateOf(false)
        private set
    var readyFresh by mutableStateOf(false)
        private set
    var questionIndex by mutableIntStateOf(0)
        private set
    var answers by mutableStateOf<Map<String, String>>(emptyMap())
        private set

    private var key: ByteArray? = null
    private var api: HubApi? = null
    private var eventJob: Job? = null
    private var generation = 0
    private var cursor = 0L
    private var currentText = false

    fun statusMessage(message: String) { status = message }

    fun updateDraft(value: String) {
        draft = value.take(58_000)
        prefs.edit().putString("draft", draft).apply()
    }

    fun setKey(value: ByteArray) {
        key?.fill(0)
        key = value
        keySelected = true
        status = "Shared key loaded for this app run"
    }

    fun connect() {
        if (busy || connected) return
        scope.launch {
            busy = true
            try {
                val normalized = Wire.origin(origin)
                val next = HubApi(normalized, key)
                val inventory = next.sessions()
                api = next
                origin = normalized
                prefs.edit().putString("hub", normalized).apply()
                connected = true
                sessions = inventory
                status = if (inventory.isEmpty()) "No live sessions" else "Connected"
                val remembered = prefs.getString("session", null)
                inventory.firstOrNull { it.id == remembered && (!it.encrypted || keySelected) }
                    ?.let { select(it) }
            } catch (error: Exception) { status = error.message ?: "Connection failed" }
            finally { busy = false }
        }
    }

    fun disconnect() {
        generation++
        eventJob?.cancel(); eventJob = null
        api?.close(); api = null
        key?.fill(0); key = null; keySelected = false
        connected = false; sessions = emptyList(); selected = null
        entries = emptyList(); pending = emptyList(); olderBefore = null
        uncertain = null; confirmFresh = false; readyFresh = false
        status = "Disconnected; shared key cleared"
    }

    fun close() = disconnect()

    fun refresh() {
        val client = api ?: return
        scope.launch {
            try {
                val inventory = client.sessions()
                sessions = inventory
                val active = selected
                if (active != null && inventory.none { it.id == active.id && it.encrypted == active.encrypted }) {
                    eventJob?.cancel(); selected = null; entries = emptyList(); pending = emptyList()
                    status = "Session went offline"
                } else status = "Session list refreshed"
            } catch (error: Exception) { status = error.message ?: "Refresh failed" }
        }
    }

    fun select(session: Session) {
        val client = api ?: return
        if (session.encrypted && !keySelected) { status = "Choose the shared key to open this session"; return }
        if (uncertain != null) {
            status = "Resolve the uncertain request before reopening a session"
            return
        }
        generation++
        val mine = generation
        eventJob?.cancel(); eventJob = null
        selected = session; entries = emptyList(); pending = emptyList(); olderBefore = null
        uncertain = null; readyFresh = false; questionIndex = 0; answers = emptyMap(); currentText = false
        prefs.edit().putString("session", session.id).apply()
        status = "Loading ${session.title}"
        scope.launch {
            try {
                snapshot(client, session, mine)
                if (mine == generation) follow(client, session, mine)
            } catch (error: Exception) {
                if (mine == generation) status = error.message ?: "Session unavailable"
            }
        }
    }

    private fun turnEntries(turns: JSONArray): List<Entry> {
        require(turns.length() <= 80) { "Invalid remote transcript" }
        val result = mutableListOf<Entry>()
        for (i in 0 until turns.length()) {
            val turn = turns.getJSONObject(i)
            turn.optString("prompt").takeIf(String::isNotBlank)?.let { result += Entry("You", it.take(16_000)) }
            turn.optString("text").takeIf(String::isNotBlank)?.let { result += Entry("DOXA", it.take(16_000)) }
            val tools = turn.optJSONArray("tools") ?: JSONArray()
            for (j in 0 until minOf(tools.length(), 8)) {
                val tool = tools.getJSONObject(j)
                result += Entry("Tool", (tool.optString("name", "Tool") +
                    tool.optString("result").takeIf(String::isNotBlank)?.let { " · $it" }.orEmpty()).take(16_000))
            }
        }
        return result
    }

    private suspend fun snapshot(client: HubApi, session: Session, mine: Int) {
        val history = client.transcript(session.id, session.encrypted)
        if (mine != generation) return
        val turns = history.getJSONArray("turns")
        val inputs = history.getJSONArray("pending_inputs")
        require(inputs.length() <= 64) { "Invalid pending input list" }
        val next = history.getLong("next_seq")
        require(next >= 0) { "Invalid transcript cursor" }
        entries = turnEntries(turns).takeLast(300)
        pending = if (history.optBoolean("pending_inputs_complete"))
            List(inputs.length()) { inputs.getJSONObject(it) } else emptyList()
        questionIndex = 0; answers = emptyMap(); currentText = false
        olderBefore = if (history.optBoolean("has_more")) history.optLong("before", -1).takeIf { it >= 0 } else null
        cursor = next
        prefs.edit().putLong("cursor", cursor).apply()
        status = if (history.optBoolean("pending_inputs_complete"))
            "${session.title} · connected" else "Pending input review incomplete; refresh before answering"
    }

    fun older() {
        val client = api ?: return
        val session = selected ?: return
        val before = olderBefore ?: return
        val mine = generation
        scope.launch {
            try {
                val history = client.transcript(session.id, session.encrypted, before)
                if (mine != generation) return@launch
                require(history.getLong("before") < before) { "Invalid history page" }
                entries = (turnEntries(history.getJSONArray("turns")) + entries).takeLast(300)
                olderBefore = if (history.optBoolean("has_more")) history.getLong("before") else null
                status = "Older turns loaded"
            } catch (error: Exception) { if (mine == generation) status = error.message ?: "History unavailable" }
        }
    }

    private fun follow(client: HubApi, session: Session, mine: Int) {
        eventJob = scope.launch {
            while (isActive && mine == generation) {
                try {
                    client.events(session.id, session.encrypted, cursor) { frame ->
                        withContext(Dispatchers.Main.immediate) {
                            if (mine == generation) handle(frame)
                        }
                    }
                    if (mine == generation) status = "Reconnecting to events"
                } catch (_: ReplayGap) {
                    if (mine == generation) {
                        status = "Event gap; refreshing transcript"
                        try { snapshot(client, session, mine) }
                        catch (error: Exception) { status = error.message ?: "Transcript refresh failed" }
                    }
                } catch (error: CancellationException) { throw error }
                catch (error: Exception) { if (mine == generation) status = error.message ?: "Event stream interrupted" }
                delay(1_500)
            }
        }
    }

    private fun append(kind: String, text: String) {
        entries = (entries + Entry(kind, text.take(16_000))).takeLast(300)
    }

    private fun handle(frame: JSONObject) {
        if (frame.optString("type") != "event") return
        val seq = frame.getLong("seq")
        require(seq in 0 until Long.MAX_VALUE) { "Invalid event sequence" }
        if (seq < cursor) return
        val event = frame.getJSONObject("event")
        val kind = event.getString("type")
        if (kind == "replay_gap") throw ReplayGap()
        val data = event.optJSONObject("data") ?: JSONObject()
        when (kind) {
            "turn_started" -> { currentText = false; data.optString("prompt").takeIf(String::isNotBlank)?.let { append("You", it) } }
            "prompt_queued" -> append("Notice", "Queued: ${data.optString("text", "prompt")}")
            "text_delta" -> {
                val delta = data.optString("text", data.optString("delta"))
                if (currentText && entries.lastOrNull()?.kind == "DOXA") {
                    val last = entries.last()
                    entries = entries.dropLast(1) + last.copy(text = (last.text + delta).take(16_000))
                } else { append("DOXA", delta); currentText = true }
            }
            "turn_done", "turn_refused" -> {
                currentText = false
                data.optString("error", data.optString("reason")).takeIf(String::isNotBlank)
                    ?.let { append("Error", it) }
                status = "Turn finished"
            }
            "needs_input" -> {
                val id = data.optString("id")
                if (Wire.id(id)) pending = (pending.filterNot { it.optString("id") == id } + data).takeLast(64)
                questionIndex = 0; answers = emptyMap(); status = "Input needed"
            }
            "needs_input_resolved" -> {
                pending = pending.filterNot { it.optString("id") == data.optString("id") }
                questionIndex = 0; answers = emptyMap()
            }
            "tool_call" -> append("Tool", data.optString("name", data.optString("tool_name", "Tool")))
            "tool_result" -> data.optString("result").takeIf(String::isNotBlank)?.let { append("Tool", it) }
        }
        alerts.onEvent(kind)
        cursor = maxOf(cursor, seq + 1)
        prefs.edit().putLong("cursor", cursor).apply()
    }

    fun sendPrompt() {
        val session = selected ?: return
        val text = draft.trim()
        if (text.isEmpty() || busy || uncertain != null) return
        send(JSONObject().put("text", text), "prompt", session)
    }

    fun chooseOption(question: JSONObject, label: String) {
        answers = answers + (question.optString("question", question.optString("header")) to label)
        questionIndex++
        val questions = pending.firstOrNull()?.optJSONArray("questions") ?: JSONArray()
        if (questionIndex >= questions.length()) sendAnswer(JSONObject().put("answers", JSONObject(answers)))
    }

    fun sendAnswer(answer: JSONObject) {
        val session = selected ?: return
        val item = pending.firstOrNull() ?: return
        val id = item.optString("id")
        if (!Wire.id(id) || busy || uncertain != null) return
        scope.launch {
            busy = true
            try {
                val client = api ?: return@launch
                // Re-read authoritative pending inputs; an SSE event alone cannot authorize an answer.
                val latest = client.transcript(session.id, session.encrypted)
                require(latest.optBoolean("pending_inputs_complete")) { "Pending input review incomplete" }
                val latestInputs = latest.getJSONArray("pending_inputs")
                val current = (0 until latestInputs.length()).map { latestInputs.getJSONObject(it) }
                    .firstOrNull { it.optString("id") == id }
                require(current != null && current.toString() == item.toString()) {
                    "Pending input changed; review it again"
                }
                val payload = JSONObject().put("id", id).put("answer", answer)
                val command = client.prepare(session.id, "answer", payload, session.encrypted)
                uncertain = command; readyFresh = false
                val result = client.submit(command, session.encrypted)
                uncertain = null
                pending = pending.filterNot { it.optString("id") == id }
                questionIndex = 0; answers = emptyMap()
                status = result.optString("status", "Answer delivered")
            } catch (error: RemoteRefusal) {
                uncertain = null
                status = error.message ?: "Answer refused"
            } catch (error: Exception) { status = error.message ?: "Answer outcome uncertain" }
            finally { busy = false }
        }
    }

    private fun send(payload: JSONObject, operation: String, session: Session) {
        val client = api ?: return
        try {
            val command = client.prepare(session.id, operation, payload, session.encrypted)
            uncertain = command; readyFresh = false
            scope.launch { deliver(client, command) }
        } catch (error: Exception) { status = error.message ?: "Invalid request" }
    }

    private suspend fun deliver(client: HubApi, command: PendingCommand) {
        busy = true
        try {
            client.submit(command, command.encrypted)
            uncertain = null
            if (command.operation == "prompt" && draft.trim() == command.payload.optString("text")) updateDraft("")
            status = if (command.operation == "prompt") "Prompt delivered" else "Answer delivered"
        } catch (error: RemoteRefusal) {
            uncertain = null
            status = error.message ?: "Request refused"
        } catch (error: Exception) { status = error.message ?: "Request outcome uncertain" }
        finally { busy = false }
    }

    fun retry() {
        val command = uncertain ?: return
        val client = api ?: return
        if (busy || selected?.id != command.target) return
        if (System.currentTimeMillis() - command.createdAt >= 120_000) {
            scope.launch {
                busy = true
                try {
                    val session = selected ?: return@launch
                    snapshot(client, session, generation)
                    status = "Review the refreshed transcript before a new request"
                    readyFresh = true
                } catch (error: Exception) { status = error.message ?: "Review unavailable" }
                finally { busy = false }
            }
        } else scope.launch { deliver(client, command) }
    }

    fun cancelFresh() { confirmFresh = false }
    fun askFresh() { if (readyFresh && uncertain != null) confirmFresh = true }

    fun freshAfterReview() {
        val prior = uncertain ?: return
        val client = api ?: return
        val session = selected ?: return
        confirmFresh = false; readyFresh = false
        if (busy || session.id != prior.target) return
        scope.launch {
            busy = true
            try {
                if (prior.operation == "answer") {
                    require(pending.any { it.optString("id") == prior.payload.optString("id") }) {
                        "The pending input is gone; no new answer sent"
                    }
                }
                val fresh = client.prepare(session.id, prior.operation, prior.payload, session.encrypted)
                uncertain = fresh
                client.submit(fresh, session.encrypted)
                uncertain = null
                if (prior.operation == "prompt" && draft.trim() == prior.payload.optString("text")) updateDraft("")
                status = "New request delivered"
            } catch (error: RemoteRefusal) {
                uncertain = null
                status = error.message ?: "New request refused"
            } catch (error: Exception) { status = error.message ?: "New request failed" }
            finally { busy = false }
        }
    }
}

class MainActivity : ComponentActivity() {
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main.immediate)
    private lateinit var state: RemoteController
    private lateinit var alerts: LocalAlerts
    private val notifications = registerForActivityResult(ActivityResultContracts.RequestPermission()) { granted ->
        alerts.updateEnabled(granted)
    }
    private val chooseKey = registerForActivityResult(ActivityResultContracts.OpenDocument()) { uri ->
        if (uri != null) try {
            val bytes = ByteArrayOutputStream()
            contentResolver.openInputStream(uri)?.use { input ->
                val chunk = ByteArray(128)
                while (true) {
                    val count = input.read(chunk)
                    if (count < 0) break
                    require(bytes.size() + count <= 128) { "Key file exceeds bound" }
                    bytes.write(chunk, 0, count)
                }
            } ?: error("Key file unavailable")
            state.setKey(Wire.key(bytes.toString(Charsets.UTF_8.name())))
        } catch (error: Exception) { state.statusMessage(error.message ?: "Invalid key file") }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val prefs = getSharedPreferences("doxa-remote", MODE_PRIVATE)
        alerts = LocalAlerts(this, prefs)
        state = RemoteController(prefs, scope, alerts)
        setContent { MaterialTheme {
            RemoteScreen(state, alerts,
                onChooseKey = { chooseKey.launch(arrayOf("text/plain", "application/octet-stream")) },
                onEnableAlerts = {
                    if (Build.VERSION.SDK_INT >= 33 &&
                        checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED)
                        notifications.launch(Manifest.permission.POST_NOTIFICATIONS)
                    else alerts.updateEnabled(true)
                })
        } }
    }

    override fun onStart() { super.onStart(); if (::alerts.isInitialized) alerts.visible = true }
    override fun onStop() { if (::alerts.isInitialized) alerts.visible = false; super.onStop() }

    override fun onDestroy() {
        state.close()
        scope.cancel()
        super.onDestroy()
    }
}

@Composable
private fun RemoteScreen(state: RemoteController, alerts: LocalAlerts,
                         onChooseKey: () -> Unit, onEnableAlerts: () -> Unit) {
    val session = state.selected
    val question = state.pending.firstOrNull()
    Column(Modifier.fillMaxSize().padding(16.dp)) {
        Text("DOXA Remote", style = MaterialTheme.typography.headlineSmall)
        Text(state.status, style = MaterialTheme.typography.bodySmall)
        Spacer(Modifier.height(8.dp))
        if (!state.connected) {
            OutlinedTextField(value = state.origin, onValueChange = { state.origin = it },
                label = { Text("Private Tailscale hub URL") }, singleLine = true,
                modifier = Modifier.fillMaxWidth())
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                OutlinedButton(onClick = onChooseKey) { Text(if (state.keySelected) "Replace key" else "Choose shared key") }
                Button(onClick = state::connect, enabled = !state.busy) { Text("Connect") }
            }
            Text("Key stays in memory for this app run. Tailscale signs in the device.",
                style = MaterialTheme.typography.bodySmall)
        } else {
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                OutlinedButton(onClick = state::refresh, enabled = !state.busy) { Text("Refresh") }
                OutlinedButton(onClick = state::disconnect, enabled = !state.busy) { Text("Disconnect") }
            }
            OutlinedButton(onClick = { if (alerts.enabled) alerts.updateEnabled(false) else onEnableAlerts() }) {
                Text(if (alerts.enabled) "Local alerts on" else "Enable local alerts")
            }
            Text("Alerts need this app's live connection; they stop if Android closes it.",
                style = MaterialTheme.typography.bodySmall)
            LazyRow(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                items(state.sessions, key = { it.id }) { item ->
                    FilterChip(selected = session?.id == item.id, onClick = { state.select(item) },
                        enabled = !item.encrypted || state.keySelected,
                        label = { Text("${if (item.encrypted) "🔒 " else ""}${item.title}") })
                }
            }
            if (session != null) {
                Text("${session.engine} · ${session.id}", style = MaterialTheme.typography.bodySmall)
                state.olderBefore?.let {
                    TextButton(onClick = state::older) { Text("Load older turns") }
                }
                LazyColumn(Modifier.weight(1f).fillMaxWidth()) {
                    items(state.entries) { entry ->
                        Card(Modifier.fillMaxWidth().padding(vertical = 3.dp)) {
                            Column(Modifier.padding(10.dp)) {
                                Text(entry.kind, style = MaterialTheme.typography.labelMedium)
                                Text(entry.text, style = MaterialTheme.typography.bodyMedium)
                            }
                        }
                    }
                }
                if (question != null) PendingInput(state, question)
                HorizontalDivider()
                OutlinedTextField(value = state.draft, onValueChange = state::updateDraft,
                    label = { Text("Prompt") }, minLines = 2, maxLines = 5,
                    enabled = !state.busy,
                    modifier = Modifier.fillMaxWidth())
                Button(onClick = state::sendPrompt, enabled = !state.busy && state.uncertain == null && state.draft.isNotBlank()) {
                    Text("Send prompt")
                }
                if (state.uncertain != null) {
                    Text("Request outcome uncertain. Review the session before retrying.",
                        style = MaterialTheme.typography.bodySmall)
                    OutlinedButton(onClick = if (state.readyFresh) state::askFresh else state::retry,
                        enabled = !state.busy) {
                        Text(if (state.readyFresh) "Send new request" else "Retry same request")
                    }
                }
            } else {
                Text("Choose a live session", modifier = Modifier.padding(top = 16.dp))
            }
        }
    }
    if (state.confirmFresh) AlertDialog(onDismissRequest = state::cancelFresh,
        title = { Text("Send a new request?") },
        text = { Text("The earlier result is uncertain. Review the refreshed transcript; a new request may repeat the action.") },
        confirmButton = { TextButton(onClick = state::freshAfterReview) { Text("Send new request") } },
        dismissButton = { TextButton(onClick = state::cancelFresh) { Text("Cancel") } })
}

@Composable
private fun PendingInput(state: RemoteController, item: JSONObject) {
    Card(Modifier.fillMaxWidth().padding(vertical = 8.dp)) {
        Column(Modifier.padding(12.dp).heightIn(max = 220.dp).verticalScroll(rememberScrollState())) {
            Text(item.optString("title", item.optString("input_summary", "Input needed")),
                style = MaterialTheme.typography.titleMedium)
            if (item.optString("kind") == "ask_user") {
                val questions = item.optJSONArray("questions") ?: JSONArray()
                if (state.questionIndex < questions.length()) {
                    val question = questions.getJSONObject(state.questionIndex)
                    Text(question.optString("question", question.optString("header")))
                    val options = question.optJSONArray("options") ?: JSONArray()
                    for (i in 0 until minOf(options.length(), 32)) {
                        val label = options.getJSONObject(i).optString("label")
                        OutlinedButton(onClick = { state.chooseOption(question, label) }, enabled = !state.busy && state.uncertain == null) {
                            Text(label)
                        }
                    }
                }
                TextButton(onClick = { state.sendAnswer(JSONObject().put("declined", true)) },
                    enabled = !state.busy && state.uncertain == null) { Text("Decline") }
            } else {
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    Button(onClick = { state.sendAnswer(JSONObject().put("decision", "allow")) },
                        enabled = !state.busy && state.uncertain == null) { Text("Allow") }
                    OutlinedButton(onClick = { state.sendAnswer(JSONObject().put("decision", "deny")) },
                        enabled = !state.busy && state.uncertain == null) { Text("Deny") }
                }
            }
        }
    }
}

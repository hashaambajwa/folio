"""Local, deterministic browser benchmark target (never binds publicly by default)."""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Folio Tasks | Evaluation Fixture</title>
<style>
:root{font-family:system-ui,Arial,sans-serif;color:#172033;background:#f4f7fb}
body{margin:0;display:grid;place-items:start center;min-height:100vh;padding:48px 16px}
main{width:min(700px,100%);background:#fff;border:1px solid #e4eaf0;border-radius:16px;
padding:28px;box-shadow:0 16px 40px #152b4312}
h1{margin:0 0 8px;font-size:28px}p{color:#526174}
.controls{display:flex;gap:8px;margin:18px 0}input{flex:1;min-width:0;padding:12px;
border:1px solid #b9c9d8;border-radius:8px;font:inherit}
button{padding:10px 14px;border:0;border-radius:8px;background:#165fc1;
color:white;cursor:pointer;font:inherit}button.secondary{background:#eaf1fb;color:#184275}
ul{list-style:none;padding:0;margin:10px 0}li{display:flex;gap:10px;align-items:center;
padding:10px 0;border-bottom:1px solid #eff2f5}li[hidden]{display:none}
li span{flex:1}li.completed span{text-decoration:line-through;color:#708094}
li input[type=checkbox]{flex:none;width:18px;height:18px}
#edit-row[hidden]{display:none}#status{font-size:13px}
</style>
</head>
<body><main>
<h1>Folio Tasks</h1>
<p>Local benchmark fixture for realistic, stateful browser actions.</p>
<div class="controls"><input id="new-task" aria-label="New task" placeholder="Add a task">
<button id="add-task">Add</button></div>
<div class="controls"><input id="filter-box" aria-label="Filter tasks" placeholder="Filter tasks">
<button id="clear-filter" class="secondary">Clear filter</button></div>
<div id="edit-row" class="controls" hidden>
<input id="edit-input" aria-label="Edit task"><button id="save-edit">Save</button>
<button id="cancel-edit" class="secondary">Cancel</button></div>
<ul id="task-list" aria-label="Task list"></ul>
<p id="status" role="status">0 tasks</p>
</main>
<script>
(() => {
 const items = [];
 let editing = null;
 const $ = id => document.getElementById(id);
 const list = $('task-list');
 function render(){
   list.replaceChildren();
   const filter = $('filter-box').value.toLowerCase();
   for(const item of items){
     const li = document.createElement('li');
     li.dataset.name = item.text;
     li.className = item.done ? 'completed' : '';
     li.hidden = !item.text.toLowerCase().includes(filter);
     const check = document.createElement('input');
     check.type = 'checkbox'; check.checked = item.done;
     check.setAttribute('aria-label', 'Complete ' + item.text);
     check.addEventListener('change', () => {item.done = check.checked; render()});
     const label = document.createElement('span'); label.textContent = item.text;
     const edit = document.createElement('button'); edit.textContent='Edit';
     edit.className='secondary edit'; edit.addEventListener('click', () => {
       editing = item; $('edit-input').value=item.text; $('edit-row').hidden=false;
       $('edit-input').focus();
     });
     const del = document.createElement('button'); del.textContent='Delete';
     del.className='secondary delete'; del.addEventListener('click', () => {
       const index=items.indexOf(item); if(index>=0)items.splice(index,1);
       if(editing===item){ editing=null; $('edit-row').hidden=true; }render();
     });
     li.append(check,label,edit,del);list.append(li);
   }
   $('status').textContent = items.length + ' tasks';
 }
 function add(){
   const text=$('new-task').value.trim();
   if(!text)return;
   items.push({text,done:false});$('new-task').value='';render();
 }
 $('add-task').addEventListener('click',add);
 $('new-task').addEventListener('keydown',e=>{if(e.key==='Enter')add()});
 $('filter-box').addEventListener('input',render);
 $('clear-filter').addEventListener('click',()=>{$('filter-box').value='';render()});
 $('save-edit').addEventListener('click',()=>{
   const text=$('edit-input').value.trim();
   if(editing && text)editing.text=text;
   editing=null;$('edit-row').hidden=true;render();
 });
 $('cancel-edit').addEventListener('click',()=>{editing=null;$('edit-row').hidden=true});
 render();
})();
</script>
</body></html>"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path.split("?", 1)[0] not in ("/", "/index.html", "/health"):
            self.send_error(404)
            return
        if self.path.startswith("/health"):
            data, mime = b"ok", "text/plain"
        else:
            data, mime = HTML.encode("utf-8"), "text/html; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *_args) -> None:
        pass


def server(host: str = "127.0.0.1", port: int = 8765) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), Handler)


def serve(host: str = "127.0.0.1", port: int = 8765) -> None:
    print(f"Folio benchmark target: http://{host}:{port}", flush=True)
    with server(host, port) as httpd:
        httpd.serve_forever()


if __name__ == "__main__":
    serve()

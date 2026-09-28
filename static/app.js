const messages=[];
let conversationId=null;
let conversations=[];
let pendingConversationModel=null;
let activeController=null;
let isGenerating=false;

const body=document.body;
const conversationList=document.getElementById("conversation-list");
const newChatButton=document.getElementById("new-chat");
const clearButton=document.getElementById("clear");
const messagesEl=document.getElementById("messages");
const emptyState=document.getElementById("empty-state");
const chatScroll=document.getElementById("chat-scroll");
const input=document.getElementById("input");
const modelSelect=document.getElementById("model");
const composer=document.getElementById("composer");
const sendButton=composer.querySelector(".send-button");
const chatStatus=document.getElementById("chat-status");
const settingsButton=document.getElementById("settings");
const configDialog=document.getElementById("config-dialog");
const configForm=document.getElementById("config-form");
const baseUrlInput=document.getElementById("base-url");
const apiKeyInput=document.getElementById("api-key");
const apiKeyRevealButton=document.getElementById("api-key-reveal");
const modelsInput=document.getElementById("models");
const commonModelSelect=document.getElementById("common-model");
const configError=document.getElementById("config-error");
const configCancelButton=document.getElementById("config-cancel");
const configCloseButton=document.getElementById("config-close");
const menuButton=document.getElementById("menu-button");
const sidebarCloseButton=document.getElementById("sidebar-close");
const sidebarBackdrop=document.getElementById("sidebar-backdrop");
const loginScreen=document.getElementById("login-screen");
const loginForm=document.getElementById("login-form");
const loginPassword=document.getElementById("login-password");
const loginError=document.getElementById("login-error");
const logoutButton=document.getElementById("logout");

async function apiFetch(url,options){
  const response=await fetch(url,options);
  if(response.status===401){showLogin();throw new Error("请先登录")}
  return response;
}

function showLogin(){loginScreen.hidden=false;loginPassword.focus()}
function hideLogin(){loginScreen.hidden=true}
function scrollToBottom(){chatScroll.scrollTop=chatScroll.scrollHeight}
function updateEmptyState(){emptyState.classList.toggle("hidden",messages.length>0)}
function setStatus(text="",kind=""){chatStatus.textContent=text;chatStatus.className=`chat-status${kind?` ${kind}`:""}`}

function appendInline(parent,text){
  const pattern=/(\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)|`([^`]+)`|\*\*([^*]+)\*\*|\*([^*]+)\*)/g;
  let cursor=0;
  for(const match of text.matchAll(pattern)){
    parent.append(document.createTextNode(text.slice(cursor,match.index)));
    let element;
    if(match[2]){
      element=document.createElement("a");
      element.textContent=match[2];
      element.href=match[3];
      element.target="_blank";
      element.rel="noopener noreferrer";
    }else if(match[4]){
      element=document.createElement("code");
      element.textContent=match[4];
    }else if(match[5]){
      element=document.createElement("strong");
      element.textContent=match[5];
    }else{
      element=document.createElement("em");
      element.textContent=match[6];
    }
    parent.append(element);
    cursor=match.index+match[0].length;
  }
  parent.append(document.createTextNode(text.slice(cursor)));
}

function renderMarkdown(container,text){
  container.innerHTML="";
  const fragments=text.split(/```/);
  fragments.forEach((fragment,index)=>{
    if(index%2===1){
      const newline=fragment.indexOf("\n");
      const language=(newline>=0?fragment.slice(0,newline):"").trim();
      const code=newline>=0?fragment.slice(newline+1):fragment;
      const wrapper=document.createElement("div");
      wrapper.className="code-block";
      const header=document.createElement("div");
      header.className="code-header";
      const label=document.createElement("span");
      label.textContent=language||"code";
      const copy=document.createElement("button");
      copy.type="button";
      copy.textContent="复制";
      copy.addEventListener("click",()=>copyText(code,copy));
      header.append(label,copy);
      const pre=document.createElement("pre");
      const codeElement=document.createElement("code");
      codeElement.textContent=code.replace(/\n$/,"");
      pre.append(codeElement);
      wrapper.append(header,pre);
      container.append(wrapper);
      return;
    }

    const lines=fragment.split("\n");
    let list=null;
    for(const line of lines){
      const heading=line.match(/^(#{1,3})\s+(.+)/);
      const listItem=line.match(/^[-*]\s+(.+)/);
      if(listItem){
        if(!list){list=document.createElement("ul");container.append(list)}
        const item=document.createElement("li");
        appendInline(item,listItem[1]);
        list.append(item);
        continue;
      }
      list=null;
      if(heading){
        const element=document.createElement(`h${Math.min(heading[1].length+1,4)}`);
        appendInline(element,heading[2]);
        container.append(element);
      }else if(line.trim()){
        const paragraph=document.createElement("p");
        appendInline(paragraph,line);
        container.append(paragraph);
      }
    }
  });
}

async function copyText(text,button){
  try{
    await navigator.clipboard.writeText(text);
    const original=button.textContent;
    button.textContent="已复制";
    setTimeout(()=>{button.textContent=original},1200);
  }catch(error){setStatus("复制失败，请手动选择文字。","error")}
}

function createAction(label,handler){
  const button=document.createElement("button");
  button.type="button";
  button.className="message-action";
  button.textContent=label;
  button.addEventListener("click",handler);
  return button;
}

function appendMessage(role,text,options={}){
  const article=document.createElement("article");
  article.className=`message ${role}${options.loading?" loading":""}${options.error?" error":""}`;
  if(role==="assistant"){
    const avatar=document.createElement("div");
    avatar.className="assistant-avatar";
    avatar.textContent="A";
    article.appendChild(avatar);
  }
  const bodyElement=document.createElement("div");
  bodyElement.className="message-body";
  const content=document.createElement("div");
  content.className="message-content";
  if(role==="assistant")renderMarkdown(content,text);else content.textContent=text;
  bodyElement.appendChild(content);
  if(!options.loading){
    const actions=document.createElement("div");
    actions.className="message-actions";
    actions.append(createAction("复制",()=>copyText(text,actions.firstElementChild)));
    if(role==="assistant"&&options.retry)actions.append(createAction("重试",options.retry));
    bodyElement.appendChild(actions);
  }
  article.appendChild(bodyElement);
  messagesEl.appendChild(article);
  updateEmptyState();
  scrollToBottom();
  return {article,content,bodyElement};
}

function renderMessages(){
  messagesEl.innerHTML="";
  messages.forEach((item,index)=>appendMessage(item.role,item.content,{retry:item.role==="assistant"?()=>retryAssistant(index):null}));
  updateEmptyState();
}

function renderConversations(){
  conversationList.innerHTML="";
  if(!conversations.length){const empty=document.createElement("div");empty.className="history-empty";empty.textContent="还没有历史对话";conversationList.appendChild(empty);return}
  for(const item of conversations){
    const button=document.createElement("button");
    button.className=`conversation-item${item.id===conversationId?" active":""}`;
    button.type="button";
    button.textContent=item.title||"新对话";
    button.title=item.title||"新对话";
    button.addEventListener("click",()=>loadConversation(item.id));
    conversationList.appendChild(button);
  }
}

function closeSidebar(){body.classList.remove("sidebar-open")}
function resetConversation(){if(isGenerating)stopGeneration();conversationId=null;messages.length=0;renderMessages();renderConversations();closeSidebar();setStatus();input.focus()}

function updateSendButton(){
  sendButton.classList.toggle("stop",isGenerating);
  sendButton.setAttribute("aria-label",isGenerating?"停止生成":"发送消息");
  sendButton.innerHTML=isGenerating?'<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="7" y="7" width="10" height="10" rx="1"/></svg>':'<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 19V5M6.5 10.5 12 5l5.5 5.5"/></svg>';
}

function setGenerating(generating){
  isGenerating=generating;
  modelSelect.disabled=generating;
  newChatButton.disabled=generating;
  clearButton.disabled=generating;
  updateSendButton();
}

async function loadConversations(){
  try{const response=await apiFetch("/api/conversations");if(!response.ok)throw new Error("无法加载历史对话");const data=await response.json();conversations=data.conversations||[];renderConversations();if(!conversationId&&conversations.length&&messages.length===0)await loadConversation(conversations[0].id)}catch(error){console.error(error)}
}

async function loadConversation(id){
  if(isGenerating)stopGeneration();
  try{const response=await apiFetch(`/api/conversations/${encodeURIComponent(id)}`);if(!response.ok)throw new Error("无法加载对话");const data=await response.json();conversationId=data.id;messages.length=0;messages.push(...(data.messages||[]));pendingConversationModel=data.model||null;selectModel(pendingConversationModel,true);renderMessages();renderConversations();closeSidebar();setStatus()}catch(error){setStatus(error.message,"error")}
}

function setModelPlaceholder(text){modelSelect.innerHTML="";modelSelect.appendChild(new Option(text,""))}
function selectModel(model,allowMissing=false){if(!model)return false;const options=Array.from(modelSelect.options);if(!options.some((option)=>option.value===model)){if(!allowMissing)return false;modelSelect.appendChild(new Option(`${model}（历史）`,model))}modelSelect.value=model;localStorage.setItem("chat-model",model);return true}

async function persistSelectedModel(model){
  if(!model)return;
  const response=await apiFetch("/api/config/model",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({model})});
  if(!response.ok){const data=await response.json().catch(()=>({}));throw new Error(data.error||"无法保存模型选择")}
  localStorage.setItem("chat-model",model);
}

async function loadModels(preferredModel=null){
  try{const response=await apiFetch("/api/models");const data=await response.json();if(!response.ok)throw new Error(data.error||"无法加载模型");const models=data.models||[];const current=modelSelect.value;modelSelect.innerHTML="";for(const model of models)modelSelect.appendChild(new Option(model,model));const saved=localStorage.getItem("chat-model");const conversationModel=preferredModel||pendingConversationModel;const selected=[data.default_model,current,saved,models[0]].find((model)=>model&&models.includes(model));if(conversationModel)selectModel(conversationModel,true);else if(selected)selectModel(selected);else setModelPlaceholder("未配置模型");pendingConversationModel=null;modelSelect.title=data.source==="relay"?"模型来自中转站（包含手动模型）":"使用手动配置的模型"}catch(error){setModelPlaceholder("连接中转站");console.error(error)}
}

function stopGeneration(){
  if(!activeController)return;
  activeController.abort();
  setStatus("已停止生成");
}

function retryAssistant(index){
  if(isGenerating)return;
  messages.splice(index,1);
  renderMessages();
  requestCompletion();
}

async function sendMessage(){
  if(isGenerating){stopGeneration();return}
  const text=input.value.trim();
  if(!text)return;
  if(!modelSelect.value){openConfigDialog();configError.textContent="请先配置中转站并选择一个模型。";return}
  input.value="";
  input.style.height="auto";
  messages.push({role:"user",content:text});
  appendMessage("user",text);
  await requestCompletion();
}

async function requestCompletion(){
  if(!messages.length||messages[messages.length-1].role!=="user")return;
  setGenerating(true);
  setStatus("AI 正在生成回复…","busy");
  activeController=new AbortController();
  const assistant=appendMessage("assistant","",{loading:true});
  let reply="";
  let failed=false;
  try{
    const response=await apiFetch("/api/chat",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({conversation_id:conversationId,messages,model:modelSelect.value}),signal:activeController.signal});
    if(!response.ok){const data=await response.json().catch(()=>({}));throw new Error(data.error||"请求失败")}
    const reader=response.body.getReader();
    const decoder=new TextDecoder();
    let buffer="";
    while(true){
      const {value,done}=await reader.read();
      if(done)break;
      buffer+=decoder.decode(value,{stream:true});
      const parts=buffer.split("\n\n");
      buffer=parts.pop();
      for(const part of parts){
        if(!part.startsWith("data:"))continue;
        const payloadText=part.slice(5).trim();
        if(payloadText==="[DONE]")continue;
        const payload=JSON.parse(payloadText);
        if(payload.conversation_id)conversationId=payload.conversation_id;
        if(payload.error)throw new Error(payload.error);
        reply+=payload.content||"";
        renderMarkdown(assistant.content,reply);
        scrollToBottom();
      }
    }
    if(reply)messages.push({role:"assistant",content:reply});
    setStatus();
  }catch(error){
    if(error.name==="AbortError"){
      if(reply)messages.push({role:"assistant",content:reply});
      else assistant.article.remove();
    }else{
      failed=true;
      assistant.article.classList.add("error");
      assistant.content.textContent=`发送失败：${error.message}`;
      setStatus("发送失败，可以点击“重试”。","error");
    }
  }finally{
    assistant.article.classList.remove("loading");
    if(assistant.article.isConnected){
      const actions=document.createElement("div");
      actions.className="message-actions";
      if(reply)actions.append(createAction("复制",()=>copyText(reply,actions.firstElementChild)));
      const assistantIndex=reply?messages.length-1:null;
      actions.append(createAction("重试",()=>assistantIndex===null?requestCompletion():retryAssistant(assistantIndex)));
      assistant.bodyElement.appendChild(actions);
    }
    activeController=null;
    setGenerating(false);
    input.focus();
    if(!failed&&reply)await loadConversations();
    scrollToBottom();
  }
}

async function openConfigDialog(){
  let saved={};
  try{saved=JSON.parse(localStorage.getItem("chat-config")||"{}")}catch(error){console.error(error)}
  try{const response=await apiFetch("/api/config");if(response.ok)saved={...saved,...await response.json()}}catch(error){console.error(error)}
  baseUrlInput.value=saved.base_url||saved.baseUrl||"";
  baseUrlInput.dataset.savedValue=baseUrlInput.value;
  apiKeyInput.value="";
  apiKeyInput.type="password";
  apiKeyRevealButton.textContent="显示";
  apiKeyRevealButton.hidden=!saved.api_key_saved||saved.key_reveal_enabled===false;
  apiKeyInput.placeholder=saved.api_key_saved?`已保存（${saved.api_key_hint}），留空则保持不变`:"sk-...";
  const commonValues=Array.from(commonModelSelect.options).map((option)=>option.value);
  commonModelSelect.value=commonValues.includes(saved.default_model)?saved.default_model:"";
  modelsInput.value=(saved.models||[]).filter((model)=>model!==commonModelSelect.value).join(", ");
  configError.textContent="";
  configDialog.showModal();
}

async function saveConfig(){
  const baseUrl=baseUrlInput.value.trim().replace(/\/$/,"");const apiKey=apiKeyInput.value.trim();const commonModel=commonModelSelect.value;const customModels=modelsInput.value.split(",").map((item)=>item.trim()).filter(Boolean);const models=commonModel?[commonModel,...customModels]:customModels;const defaultModel=commonModel||customModels[0]||modelSelect.value;const submitButton=configForm.querySelector("button[type='submit']");configError.textContent="";submitButton.disabled=true;submitButton.textContent="正在连接...";
  try{const modelOnly=!apiKey&&defaultModel&&baseUrl===baseUrlInput.dataset.savedValue;const payload=modelOnly?{model:defaultModel,models}:{base_url:baseUrl,models,default_model:defaultModel};if(apiKey&&!modelOnly)payload.api_key=apiKey;const response=await apiFetch(modelOnly?"/api/config/model":"/api/config",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(payload)});const data=await response.json().catch(()=>({}));if(!response.ok)throw new Error(data.error||"配置保存失败");localStorage.setItem("chat-config",JSON.stringify({baseUrl,models}));if(defaultModel)localStorage.setItem("chat-model",defaultModel);await loadModels(defaultModel||null);return true}finally{apiKeyInput.value="";apiKeyInput.type="password";submitButton.disabled=false;submitButton.textContent="保存并连接"}
}

async function toggleApiKeyVisibility(){
  if(apiKeyInput.type==="text"){apiKeyInput.type="password";apiKeyRevealButton.textContent="显示";return}
  if(!apiKeyInput.value){
    apiKeyRevealButton.disabled=true;
    try{const response=await apiFetch("/api/config/key",{headers:{"X-Reveal-Api-Key":"1"}});const data=await response.json().catch(()=>({}));if(!response.ok)throw new Error(data.error||"无法读取 API Key");apiKeyInput.value=data.api_key||""}catch(error){configError.textContent=error.message;return}finally{apiKeyRevealButton.disabled=false}
  }
  apiKeyInput.type="text";
  apiKeyRevealButton.textContent="隐藏";
}

settingsButton.addEventListener("click",openConfigDialog);
configCancelButton.addEventListener("click",()=>configDialog.close());
configCloseButton.addEventListener("click",()=>configDialog.close());
configForm.addEventListener("submit",async(event)=>{event.preventDefault();try{if(await saveConfig())configDialog.close()}catch(error){configError.textContent=error.message}});
configDialog.addEventListener("click",(event)=>{if(event.target===configDialog)configDialog.close()});
configDialog.addEventListener("close",()=>{apiKeyInput.value="";apiKeyInput.type="password";apiKeyRevealButton.textContent="显示"});
apiKeyRevealButton.addEventListener("click",toggleApiKeyVisibility);
modelSelect.addEventListener("change",()=>{persistSelectedModel(modelSelect.value).catch((error)=>setStatus(error.message,"error"))});
composer.addEventListener("submit",(event)=>{event.preventDefault();sendMessage()});
input.addEventListener("keydown",(event)=>{if(event.key==="Enter"&&!event.shiftKey&&!event.isComposing){event.preventDefault();sendMessage()}else if(event.key==="Escape"&&isGenerating)stopGeneration()});
input.addEventListener("input",()=>{input.style.height="auto";input.style.height=Math.min(input.scrollHeight,180)+"px"});
newChatButton.addEventListener("click",resetConversation);
clearButton.addEventListener("click",resetConversation);
menuButton.addEventListener("click",()=>body.classList.add("sidebar-open"));
sidebarCloseButton.addEventListener("click",closeSidebar);
sidebarBackdrop.addEventListener("click",closeSidebar);
loginForm.addEventListener("submit",async(event)=>{event.preventDefault();loginError.textContent="";try{const response=await fetch("/api/login",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({password:loginPassword.value})});const data=await response.json().catch(()=>({}));if(!response.ok)throw new Error(data.error||"登录失败");loginPassword.value="";hideLogin();logoutButton.hidden=false;await initializeApp()}catch(error){loginError.textContent=error.message}});
logoutButton.addEventListener("click",async()=>{await fetch("/api/logout",{method:"POST"}).catch(console.error);showLogin()});
document.querySelectorAll(".suggestion").forEach((button)=>button.addEventListener("click",()=>{input.value=button.dataset.prompt||"";input.dispatchEvent(new Event("input"));input.focus()}));

async function initializeApp(){updateEmptyState();setModelPlaceholder("连接中转站");await Promise.all([loadConversations(),loadModels()])}
async function startApp(){
  if("serviceWorker" in navigator)navigator.serviceWorker.register("/sw.js").catch(console.error);
  const oldConfig=localStorage.getItem("chat-config");
  if(oldConfig){try{const saved=JSON.parse(oldConfig);localStorage.setItem("chat-config",JSON.stringify({baseUrl:saved.baseUrl||"",models:saved.models||[]}))}catch(error){localStorage.removeItem("chat-config")}}
  try{const response=await fetch("/api/session");const session=await response.json();if(session.auth_required&&!session.authenticated){showLogin();return}logoutButton.hidden=!session.auth_required;await initializeApp()}catch(error){console.error(error)}
}

updateSendButton();
startApp();

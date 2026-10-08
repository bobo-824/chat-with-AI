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
let apiKeyDraft="";
const modelsInput=document.getElementById("models");
const webSearchInput=document.getElementById("web-search");
const dictionaryToggleInput=document.getElementById("dictionary-enabled");
const visionInput=document.getElementById("vision-input");
const searchInput=document.getElementById("search-input");
const modelHint=document.getElementById("model-hint");
const attachButton=document.getElementById("attach-image");
const imageInput=document.getElementById("image-input");
const attachmentsEl=document.getElementById("attachments");
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
  let response;
  try{response=await fetch(url,options)}
  catch(error){
    if(error.name==="AbortError")throw error;
    console.error(error);
    throw new Error("无法连接到本地服务：请确认 server.py 仍在运行，然后重试");
  }
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

function assistantRecord(content,searches,sources){
  const record={role:"assistant",content};
  if(searches.length)record.search=[...searches];
  if(sources.length)record.sources=[...sources];
  return record;
}

function renderSearchTrace(parent,searches,sources){
  parent.querySelector(".search-trace")?.remove();
  if(!searches.length&&!sources.length)return;
  const trace=document.createElement("div");
  trace.className="search-trace";
  if(searches.length){
    const line=document.createElement("p");
    line.className="message-search";
    line.textContent=`联网搜索：${searches.join("、")}`;
    trace.appendChild(line);
  }
  const list=document.createElement("ul");
  list.className="message-sources";
  for(const source of sources){
    const url=String(source.url||"");
    if(!/^https?:\/\//i.test(url))continue;
    const item=document.createElement("li");
    const link=document.createElement("a");
    link.href=url;
    link.target="_blank";
    link.rel="noopener noreferrer";
    link.textContent=String(source.title||url).slice(0,120);
    item.appendChild(link);
    if(source.snippet){
      const note=document.createElement("span");
      note.textContent=String(source.snippet).slice(0,160);
      item.appendChild(note);
    }
    list.appendChild(item);
  }
  if(list.children.length)trace.appendChild(list);
  const actions=parent.querySelector(".message-actions");
  if(actions)parent.insertBefore(trace,actions);else parent.appendChild(trace);
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
  if(options.images&&options.images.length){
    const strip=document.createElement("div");
    strip.className="message-images";
    for(const image of options.images){
      const picture=document.createElement("img");
      picture.src=image;
      picture.alt="随消息携带的图片";
      picture.loading="lazy";
      strip.append(picture);
    }
    bodyElement.append(strip);
  }
  const content=document.createElement("div");
  content.className="message-content";
  if(role==="assistant")renderMarkdown(content,text);else content.textContent=text;
  bodyElement.appendChild(content);
  if(options.searches||options.sources)renderSearchTrace(bodyElement,options.searches||[],options.sources||[]);
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
  closeDictionaryCard();
  messagesEl.innerHTML="";
  messages.forEach((item,index)=>appendMessage(item.role,item.content,{retry:item.role==="assistant"?()=>retryAssistant(index):null,searches:item.search||[],sources:item.sources||[],images:item.images||[]}));
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
function resetConversation(){if(isGenerating)stopGeneration();conversationId=null;messages.length=0;pendingImage=null;renderAttachments();renderMessages();renderConversations();closeSidebar();setStatus();input.focus()}

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
  try{const response=await apiFetch("/api/models");const data=await response.json();if(!response.ok)throw new Error(data.error||"无法加载模型");const models=data.models||[];relayModels=Array.isArray(data.discovered_models)?data.discovered_models:[];const current=modelSelect.value;modelSelect.innerHTML="";for(const model of models)modelSelect.appendChild(new Option(model,model));const saved=localStorage.getItem("chat-model");const conversationModel=preferredModel||pendingConversationModel;const selected=[data.default_model,current,saved,models[0]].find((model)=>model&&models.includes(model));if(conversationModel)selectModel(conversationModel,true);else if(selected)selectModel(selected);else setModelPlaceholder("未配置模型");pendingConversationModel=null;refreshModelHint();modelSelect.title=data.source==="relay"?"模型来自中转站（包含手动模型）":"使用手动配置的模型"}catch(error){setModelPlaceholder("连接中转站");console.error(error)}
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
  const image=pendingImage;
  if(!text)return;
  if(!modelSelect.value){openConfigDialog();configError.textContent="请先配置中转站并选择一个模型。";return}
  input.value="";
  input.style.height="auto";
  const record={role:"user",content:text};
  if(image)record.images=[image];
  messages.push(record);
  appendMessage("user",text,{images:image?[image]:[]});
  pendingImage=null;
  renderAttachments();
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
  let streamNotice="";
  const searches=[];
  const sources=[];
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
        if(payload.notice){streamNotice=payload.notice;setStatus(payload.notice,"error")}
        if(payload.search&&payload.search.query&&searches.indexOf(payload.search.query)<0){searches.push(payload.search.query);setStatus(`正在联网搜索：${payload.search.query}`,"busy")}
        for(const source of payload.sources||[]){if(source&&source.url&&sources.every((item)=>item.url!==source.url))sources.push(source)}
        if(payload.search||payload.sources)renderSearchTrace(assistant.bodyElement,searches,sources);
        reply+=payload.content||"";
        renderMarkdown(assistant.content,reply);
        scrollToBottom();
      }
    }
    if(reply)messages.push(assistantRecord(reply,searches,sources));
    setStatus(streamNotice, streamNotice?"error":"");
  }catch(error){
    if(error.name==="AbortError"){
      if(reply)messages.push(assistantRecord(reply,searches,sources));
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

const MAX_UPLOAD_IMAGE_BYTES=1024*1024;
const MAX_READ_IMAGE_BYTES=20*1024*1024;
let pendingImage=null;
let visionEnabled=false;
let relayModels=[];

function estimateDataUrlBytes(dataUrl){
  return Math.floor((dataUrl.length-dataUrl.indexOf(",")-1)*0.75);
}

async function decodeImage(file){
  if("createImageBitmap" in window){
    try{return await createImageBitmap(file)}catch(error){}
  }
  const url=URL.createObjectURL(file);
  try{
    const image=new Image();
    await new Promise((resolve,reject)=>{image.onload=resolve;image.onerror=()=>reject(new Error("\u56fe\u7247\u8bfb\u53d6\u5931\u8d25"));image.src=url});
    return image;
  }finally{URL.revokeObjectURL(url)}
}

async function compressImage(file){
  if(!file||(file.type&&!file.type.startsWith("image/")))throw new Error("\u53ea\u80fd\u6dfb\u52a0\u56fe\u7247\u6587\u4ef6");
  if(file.size>MAX_READ_IMAGE_BYTES)throw new Error("\u56fe\u7247\u8d85\u8fc7 20MB\uff0c\u8bf7\u6362\u4e00\u5f20");
  const source=await decodeImage(file);
  const width=source.width||source.displayWidth;
  const height=source.height||source.displayHeight;
  if(!width||!height)throw new Error("\u65e0\u6cd5\u8bfb\u53d6\u56fe\u7247\u5c3a\u5bf8");
  let scale=1;
  for(let attempt=0;attempt<4;attempt++){
    const canvas=document.createElement("canvas");
    canvas.width=Math.max(1,Math.round(width*scale));
    canvas.height=Math.max(1,Math.round(height*scale));
    const context=canvas.getContext("2d");
    context.fillStyle="#ffffff";
    context.fillRect(0,0,canvas.width,canvas.height);
    context.drawImage(source,0,0,canvas.width,canvas.height);
    const dataUrl=canvas.toDataURL("image/jpeg",Math.max(0.4,0.82-attempt*0.12));
    if(estimateDataUrlBytes(dataUrl)<=MAX_UPLOAD_IMAGE_BYTES)return dataUrl;
    scale*=0.7;
  }
  throw new Error("\u56fe\u7247\u538b\u7f29\u540e\u4ecd\u7136\u592a\u5927\uff0c\u8bf7\u6362\u4e00\u5f20\u5c0f\u4e00\u70b9\u7684\u56fe");
}

function renderAttachments(){
  attachmentsEl.textContent="";
  attachmentsEl.hidden=!pendingImage;
  if(!pendingImage)return;
  const chip=document.createElement("div");
  chip.className="attachment-chip";
  const picture=document.createElement("img");
  picture.src=pendingImage;
  picture.alt="\u5f85\u53d1\u9001\u7684\u56fe\u7247";
  const note=document.createElement("span");
  note.textContent=`\u56fe\u7247 ${Math.round(estimateDataUrlBytes(pendingImage)/1024)} KB`;
  chip.append(picture,note,createAction("\u79fb\u9664",()=>{pendingImage=null;renderAttachments();input.focus()}));
  attachmentsEl.append(chip);
}

function refreshImageControls(){
  attachButton.hidden=!visionEnabled;
  if(!visionEnabled&&pendingImage){pendingImage=null;renderAttachments()}
}

async function attachImageFile(file){
  if(!file)return;
  if(!visionEnabled){setStatus("\u8bf7\u5148\u5728\u201c\u8fde\u63a5\u8bbe\u7f6e\u201d\u91cc\u5f00\u542f\u56fe\u7247\u8f93\u5165\u3002","error");imageInput.value="";return}
  setStatus("\u6b63\u5728\u538b\u7f29\u56fe\u7247\u2026","busy");
  try{
    pendingImage=await compressImage(file);
    renderAttachments();
    setStatus();
  }catch(error){
    setStatus(error.message||"\u56fe\u7247\u8bfb\u53d6\u5931\u8d25","error");
  }finally{
    imageInput.value="";
  }
}

function refreshModelHint(){
  const typed=[commonModelSelect.value,...modelsInput.value.split(",").map((item)=>item.trim()).filter(Boolean),modelSelect.value].find((item)=>item);
  if(!typed||!relayModels.length||relayModels.includes(typed)){modelHint.hidden=true;modelHint.textContent="";return}
  modelHint.textContent=`「${typed}」没有出现在中转站返回的模型列表里。中转站页面上的显示名（例如带 2x 这类倍率标记）通常不是真正的模型 ID，请核对后重填。`;
  modelHint.hidden=false;
}

async function refreshVisionCapability(){
  try{
    const response=await apiFetch("/api/config");
    if(!response.ok)return;
    const data=await response.json();
    visionEnabled=Boolean(data.vision_input_enabled||data.vision_auto_supported);
  }catch(error){
    return;
  }
  refreshImageControls();
}

const DICTIONARY_TOKEN=/[A-Za-z][A-Za-z'\u2019-]{1,23}/g;
const DICTIONARY_SINGLE=/^[A-Za-z][A-Za-z'\u2019-]{1,23}$/;
const dictionaryCache=new Map();
const dictionaryPending=new Map();
let dictionaryCard=null;
let dictionaryPointer=null;

function dictionaryEnabled(){return localStorage.getItem("dictionary-enabled")!=="0"}
function closeDictionaryCard(){if(dictionaryCard){dictionaryCard.remove();dictionaryCard=null}}

function caretRangeAt(x,y){
  if(document.caretRangeFromPoint)return document.caretRangeFromPoint(x,y);
  const position=document.caretPositionFromPoint?document.caretPositionFromPoint(x,y):null;
  if(!position||!position.offsetNode)return null;
  const range=document.createRange();
  range.setStart(position.offsetNode,position.offset);
  range.collapse(true);
  return range;
}

function wordAt(x,y){
  const range=caretRangeAt(x,y);
  if(!range)return null;
  const node=range.startContainer;
  if(!node||node.nodeType!==Node.TEXT_NODE||!node.parentElement)return null;
  if(node.parentElement.closest("a,code,pre,button,.search-trace,.message-actions"))return null;
  const message=node.parentElement.closest(".message.assistant .message-content");
  if(!message)return null;
  const text=node.textContent;
  const offset=range.startOffset;
  DICTIONARY_TOKEN.lastIndex=0;
  let match;
  while((match=DICTIONARY_TOKEN.exec(text))){
    if(offset>=match.index&&offset<=match.index+match[0].length)return match[0];
  }
  return null;
}

function renderDictionaryCard(card,word,data){
  const phonetic=card.querySelector(".dictionary-phonetic");
  const list=card.querySelector(".dictionary-list");
  if(!list)return;
  if(phonetic)phonetic.textContent=data.phonetic?`/${data.phonetic}/`:"";
  list.textContent="";
  const senses=data.results||[];
  if(!senses.length){
    const empty=document.createElement("li");
    empty.className="dictionary-empty";
    empty.textContent=data.hint||"词典未收录该词";
    list.append(empty);
    return;
  }
  const base=String(data.query||"").toLowerCase();
  if(base&&base!==word.toLowerCase()){
    const note=document.createElement("li");
    note.className="dictionary-base";
    note.textContent=`原形：${base}`;
    list.append(note);
  }
  for(const sense of senses){
    const item=document.createElement("li");
    if(sense.pos){
      const tag=document.createElement("span");
      tag.className="dictionary-pos";
      tag.textContent=sense.pos;
      item.append(tag);
    }
    const text=document.createElement("span");
    text.className="dictionary-gloss";
    text.textContent=sense.text;
    item.append(text);
    list.append(item);
  }
}

function showDictionaryCard(word,x,y){
  closeDictionaryCard();
  const card=document.createElement("div");
  card.className="dictionary-card";
  card.setAttribute("role","dialog");
  card.setAttribute("aria-label",`\u201c${word}\u201d的中文释义`);
  const head=document.createElement("div");
  head.className="dictionary-head";
  const title=document.createElement("strong");
  title.textContent=word;
  const phonetic=document.createElement("span");
  phonetic.className="dictionary-phonetic dictionary-loading";
  phonetic.textContent="查询中";
  head.append(title,phonetic);
  const list=document.createElement("ul");
  list.className="dictionary-list";
  card.append(head,list);
  document.body.append(card);
  dictionaryCard=card;
  positionDictionaryCard(card,x,y);
  return card;
}

function positionDictionaryCard(card,x,y){
  const gap=10;
  const rect=card.getBoundingClientRect();
  const left=Math.min(Math.max(gap,x-rect.width/2),Math.max(gap,window.innerWidth-rect.width-gap));
  let top=y+gap;
  if(top+rect.height>window.innerHeight-gap)top=Math.max(gap,y-rect.height-gap);
  card.style.left=`${Math.round(left)}px`;
  card.style.top=`${Math.round(top)}px`;
}

async function requestDictionary(word){
  const key=word.toLowerCase();
  if(dictionaryCache.has(key))return dictionaryCache.get(key);
  if(dictionaryPending.has(key))return dictionaryPending.get(key);
  const request=apiFetch("/api/dictionary",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({word:key})})
    .then(async(response)=>{
      const data=await response.json().catch(()=>({}));
      if(!response.ok)throw new Error(data.error||"释义查询失败");
      if(dictionaryCache.size>60)dictionaryCache.clear();
      dictionaryCache.set(key,data);
      return data;
    })
    .finally(()=>dictionaryPending.delete(key));
  dictionaryPending.set(key,request);
  return request;
}

function lookUpWord(word,x,y){
  const clean=word.replace(/\u2019/g,"'");
  const card=showDictionaryCard(clean,x,y);
  requestDictionary(clean)
    .then((data)=>{if(dictionaryCard===card)renderDictionaryCard(card,clean,data)})
    .catch((error)=>{
      if(dictionaryCard!==card)return;
      const phonetic=card.querySelector(".dictionary-phonetic");
      const list=card.querySelector(".dictionary-list");
      if(!list)return;
      if(phonetic)phonetic.textContent="";
      list.textContent="";
      const item=document.createElement("li");
      item.className="dictionary-empty";
      item.textContent=error.message;
      list.append(item);
    });
}

function selectedWord(){
  const selection=window.getSelection?String(window.getSelection()):"";
  const text=selection.trim();
  return DICTIONARY_SINGLE.test(text)?text:"";
}

async function openConfigDialog(){
  let saved={};
  try{saved=JSON.parse(localStorage.getItem("chat-config")||"{}")}catch(error){console.error(error)}
  try{const response=await apiFetch("/api/config");if(response.ok)saved={...saved,...await response.json()}}catch(error){console.error(error)}
  baseUrlInput.value=saved.base_url||saved.baseUrl||"";
  baseUrlInput.dataset.savedValue=baseUrlInput.value;
  apiKeyInput.value=apiKeyDraft;
  apiKeyInput.type="password";
  apiKeyRevealButton.textContent="显示";
  apiKeyRevealButton.hidden=Boolean(apiKeyDraft)||!saved.api_key_saved||saved.key_reveal_enabled===false;
  apiKeyInput.placeholder=saved.api_key_saved?`已保存（${saved.api_key_hint}），留空则保持不变`:"sk-...";
  const commonValues=Array.from(commonModelSelect.options).map((option)=>option.value);
  commonModelSelect.value=commonValues.includes(saved.default_model)?saved.default_model:"";
  modelsInput.value=(saved.models||[]).filter((model)=>model!==commonModelSelect.value).join(", ");
  webSearchInput.checked=Boolean(saved.web_search_enabled);
  webSearchInput.dataset.savedState=webSearchInput.checked?"1":"0";
  dictionaryToggleInput.checked=localStorage.getItem("dictionary-enabled")!=="0";
  visionInput.checked=Boolean(saved.vision_input_enabled);
  visionInput.dataset.savedState=visionInput.checked?"1":"0";
  searchInput.checked=Boolean(saved.search_input_enabled);
  searchInput.dataset.savedState=searchInput.checked?"1":"0";
  refreshModelHint();
  configError.textContent="";
  configDialog.showModal();
}

async function saveConfig(){
  const baseUrl=baseUrlInput.value.trim().replace(/\/$/,"");const apiKey=apiKeyInput.value.trim();const keyChanged=apiKey!==apiKeyDraft;const commonModel=commonModelSelect.value;const customModels=modelsInput.value.split(",").map((item)=>item.trim()).filter(Boolean);const models=commonModel?[commonModel,...customModels]:customModels;const defaultModel=commonModel||customModels[0]||modelSelect.value;const submitButton=configForm.querySelector("button[type='submit']");configError.textContent="";localStorage.setItem("dictionary-enabled",dictionaryToggleInput.checked?"1":"0");submitButton.disabled=true;submitButton.textContent="正在连接...";
  try{const togglesChanged=[webSearchInput,visionInput,searchInput].some((box)=>box.checked!==(box.dataset.savedState==="1"));const modelOnly=!keyChanged&&!togglesChanged&&defaultModel&&baseUrl===baseUrlInput.dataset.savedValue;const payload=modelOnly?{model:defaultModel,models}:{base_url:baseUrl,models,default_model:defaultModel,web_search:webSearchInput.checked,vision_input:visionInput.checked,search_input:searchInput.checked};if(keyChanged&&apiKey)payload.api_key=apiKey;const response=await apiFetch(modelOnly?"/api/config/model":"/api/config",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(payload)});const data=await response.json().catch(()=>({}));if(!response.ok)throw new Error(data.error||"配置保存失败");localStorage.setItem("chat-config",JSON.stringify({baseUrl,models}));if(defaultModel)localStorage.setItem("chat-model",defaultModel);await loadModels(defaultModel||null);await refreshVisionCapability();if(keyChanged)apiKeyDraft=apiKey;apiKeyInput.value=apiKeyDraft;return true}finally{apiKeyInput.type="password";apiKeyRevealButton.textContent="显示";submitButton.disabled=false;submitButton.textContent="保存并连接"}
}

async function toggleApiKeyVisibility(){
  if(apiKeyInput.type==="text"){apiKeyInput.type="password";apiKeyRevealButton.textContent="显示";return}
  if(!apiKeyInput.value){
    apiKeyRevealButton.disabled=true;
    try{const response=await apiFetch("/api/config/key",{headers:{"X-Reveal-Api-Key":"1"}});const data=await response.json().catch(()=>({}));if(!response.ok)throw new Error(data.error||"无法读取 API Key");apiKeyInput.value=data.api_key||"";apiKeyDraft=apiKeyInput.value}catch(error){configError.textContent=error.message;return}finally{apiKeyRevealButton.disabled=false}
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
modelsInput.addEventListener("input",refreshModelHint);
commonModelSelect.addEventListener("change",refreshModelHint);
attachButton.addEventListener("click",()=>imageInput.click());
imageInput.addEventListener("change",()=>attachImageFile(imageInput.files&&imageInput.files[0]));
input.addEventListener("paste",(event)=>{
  const items=event.clipboardData&&event.clipboardData.items;
  if(!items)return;
  for(const item of items){
    if(item.kind==="file"&&((item.type||"").startsWith("image/")||/\.(png|jpe?g|webp)$/i.test(item.name||""))){
      const file=item.getAsFile();
      if(file){event.preventDefault();attachImageFile(file)}
      return;
    }
  }
});
composer.addEventListener("dragover",(event)=>{event.preventDefault();if(visionEnabled)composer.classList.add("dragging")});
composer.addEventListener("dragleave",()=>composer.classList.remove("dragging"));
composer.addEventListener("drop",(event)=>{
  event.preventDefault();
  composer.classList.remove("dragging");
  const file=event.dataTransfer&&event.dataTransfer.files&&event.dataTransfer.files[0];
  attachImageFile(file);
});

messagesEl.addEventListener("pointerdown",(event)=>{dictionaryPointer={x:event.clientX,y:event.clientY}});
messagesEl.addEventListener("click",(event)=>{
  if(!dictionaryEnabled())return;
  const origin=dictionaryPointer;
  dictionaryPointer=null;
  const dragged=Boolean(origin)&&(Math.abs(event.clientX-origin.x)>6||Math.abs(event.clientY-origin.y)>6);
  if(dragged||selectedWord())return;
  const word=wordAt(event.clientX,event.clientY);
  if(!word){closeDictionaryCard();return}
  lookUpWord(word,event.clientX,event.clientY);
});
messagesEl.addEventListener("dblclick",(event)=>{
  if(!dictionaryEnabled())return;
  const word=selectedWord()||wordAt(event.clientX,event.clientY);
  if(word)lookUpWord(word,event.clientX,event.clientY);
});
document.addEventListener("click",(event)=>{if(dictionaryCard&&!dictionaryCard.contains(event.target))closeDictionaryCard()},true);
document.addEventListener("keydown",(event)=>{if(event.key==="Escape")closeDictionaryCard()});
chatScroll.addEventListener("scroll",closeDictionaryCard,{passive:true});
window.addEventListener("resize",closeDictionaryCard);

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

async function initializeApp(){updateEmptyState();setModelPlaceholder("连接中转站");await Promise.all([loadConversations(),loadModels(),refreshVisionCapability()])}
async function startApp(){
  if("serviceWorker" in navigator)navigator.serviceWorker.register("/sw.js").catch(console.error);
  const oldConfig=localStorage.getItem("chat-config");
  if(oldConfig){try{const saved=JSON.parse(oldConfig);localStorage.setItem("chat-config",JSON.stringify({baseUrl:saved.baseUrl||"",models:saved.models||[]}))}catch(error){localStorage.removeItem("chat-config")}}
  try{const response=await apiFetch("/api/session");const session=await response.json();if(session.auth_required&&!session.authenticated){showLogin();return}logoutButton.hidden=!session.auth_required;await initializeApp()}catch(error){console.error(error);setStatus("当前页面无法连接本地服务（可能来自离线缓存）：请运行 python server.py，然后关闭本页面重新打开","error")}
}

updateSendButton();
startApp();

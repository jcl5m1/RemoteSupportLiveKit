"""Translation corpus for synthetic speech loop-back testing.

This module defines a small, index-aligned parallel corpus of customer
support / remote-help conversation utterances in four languages:
English (en), Simplified Mandarin Chinese (zh), Spanish (es), and
Tagalog/Filipino (tl).

Alignment contract:
    - SUPPORTED_LANGUAGES lists the language codes in canonical order.
    - SENTENCES maps each language code to a list of exactly 25 strings.
    - All four lists have the same length and are aligned by index:
      SENTENCES[lang][i] is the faithful translation of SENTENCES["en"][i]
      for every lang in SUPPORTED_LANGUAGES and every index i.
    - Strings contain no trailing punctuation and no quotation marks so
      they can be spoken directly by TTS and compared against recognized
      speech in loop-back tests.

Any edit that adds, removes, or reorders a sentence in one language must
make the corresponding edit in all other languages to preserve alignment.
"""

SUPPORTED_LANGUAGES: tuple[str, ...] = ("en", "zh", "es", "tl")

# 25 support-call-relevant sentences, each aligned across all four languages by index.
SENTENCES: dict[str, list[str]] = {
    "en": [
        "Hello, thank you for calling technical support today",
        "My computer will not turn on this morning",
        "Can you help me fix my internet connection",
        "Sure, I can walk you through the steps",
        "First, please unplug the router from the wall",
        "Wait thirty seconds, then plug it back in",
        "The lights on my modem are all red",
        "Have you tried restarting your computer already",
        "Yes, I restarted it twice, but nothing changed",
        "Let's check if your account is still active",
        "Could you read me the error message please",
        "It says the password is not correct",
        "Please try your password again",
        "That fixed it, thank you so much",
        "You're welcome, is there anything else I can help with",
        "My phone screen cracked and the touch does not work",
        "I need to cancel my subscription this month",
        "I can help you with that right now",
        "Please confirm your email address for me",
        "The technician will visit your home tomorrow morning",
        "Do you want me to send you a confirmation text",
        "Everything is working again, I really appreciate your patience",
        "Would you like me to check for software updates",
        "The camera is not showing any video during calls",
        "Thank you for your help, have a great day",
    ],
    "zh": [
        "您好，感谢您今天致电技术支持中心",
        "我的电脑今天早上开不了机",
        "您能帮我修一下网络连接吗",
        "当然可以，我可以一步一步教您操作",
        "首先，请把路由器的电源插头拔掉",
        "等三十秒，然后再插回去",
        "我调制解调器上的灯全是红色的",
        "您已经试过重启电脑了吗",
        "试过了，我重启了两次，但是没有任何变化",
        "我们先确认一下您的账户是否仍然有效",
        "请您把报错信息念给我听好吗",
        "上面显示密码不正确",
        "请再试一次你的密码",
        "好了，问题解决了，太感谢您了",
        "不客气，还有什么我可以帮您的吗",
        "我的手机屏幕碎了，触控也用不了了",
        "我这个月需要取消订阅",
        "我现在就可以帮您办理",
        "请您跟我确认一下您的邮箱地址",
        "技术人员明天上午会到您家里",
        "需要我给您发一条确认短信吗",
        "一切都恢复正常了，非常感谢您的耐心等待",
        "需要我帮您检查一下软件更新吗",
        "通话的时候摄像头不显示任何画面",
        "谢谢您的帮助，祝您今天愉快",
    ],
    "es": [
        "Hola, gracias por llamar al soporte técnico",
        "Mi computadora no enciende esta mañana",
        "Me puedes ayudar a arreglar mi conexión a internet",
        "Claro, puedo guiarte paso a paso",
        "Primero, desconecta el router de la pared",
        "Espera treinta segundos y vuelve a conectarlo",
        "Todas las luces de mi módem están en rojo",
        "Ya intentaste reiniciar tu computadora",
        "Sí, la reinicié dos veces pero no cambió nada",
        "Vamos a verificar si tu cuenta sigue activa",
        "Me puedes leer el mensaje de error por favor",
        "Dice que la contraseña no es correcta",
        "Intenta tu contraseña otra vez por favor",
        "Con eso se arregló, muchas gracias",
        "De nada, hay algo más en lo que pueda ayudarte",
        "Se me rompió la pantalla del teléfono y el táctil no funciona",
        "Necesito cancelar mi suscripción este mes",
        "Puedo ayudarte con eso ahora mismo",
        "Confírmame tu dirección de correo electrónico por favor",
        "El técnico visitará tu casa mañana por la mañana",
        "Quieres que te envíe un mensaje de confirmación",
        "Ya todo funciona de nuevo, agradezco mucho tu paciencia",
        "Quieres que revise si hay actualizaciones del software",
        "La cámara no muestra ningún video durante las llamadas",
        "Gracias por tu ayuda, que tengas un buen día",
    ],
    "tl": [
        "Kamusta, salamat sa pagtawag sa aming technical support",
        "Ayaw bumukas ng computer ko ngayong umaga",
        "Matutulungan mo ba akong ayusin ang koneksyon ko sa internet",
        "Oo naman, gabayan kita sa mga hakbang",
        "Una, tanggalin mo muna ang router sa saksakan",
        "Maghintay ng tatlumpung segundo, tapos isaksak mo ulit",
        "Pula ang lahat ng ilaw ng modem ko",
        "Nasubukan mo na bang i-restart ang computer mo",
        "Oo, dalawang beses ko nang ni-restart pero walang nagbago",
        "Tignan natin kung aktibo pa ang account mo",
        "Mababasa mo ba sa akin ang mensahe ng error",
        "Nakalagay na mali ang password",
        "Subukan mo ulit ang password mo pakiusap",
        "Ayos na, maraming salamat sa iyo",
        "Walang anuman, may iba pa ba akong matutulong sa iyo",
        "Basag ang screen ng telepono ko at ayaw gumana ng touch",
        "Kailangan kong i-kansela ang subscription ko ngayong buwan",
        "Matutulungan kita diyan ngayon din",
        "Pakikumpirma mo sa akin ang email address mo",
        "Bukas ng umaga bibisita ang technician sa bahay ninyo",
        "Gusto mo bang magpadala ako ng confirmation text sa iyo",
        "Gumagana na ulit ang lahat, salamat sa pasensya mo",
        "Gusto mo bang i-check ko kung may software updates",
        "Walang lumalabas na video ang camera tuwing tawag",
        "Salamat sa tulong mo, magandang araw sa iyo",
    ],
}

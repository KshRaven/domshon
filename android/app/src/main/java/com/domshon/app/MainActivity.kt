package com.domshon.app

import android.annotation.SuppressLint
import android.app.DownloadManager
import android.content.Context
import android.content.Intent
import android.net.ConnectivityManager
import android.net.Uri
import android.os.Bundle
import android.os.Environment
import android.text.InputType
import android.view.View
import android.webkit.*
import android.widget.EditText
import android.widget.Toast
import androidx.activity.OnBackPressedCallback
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import androidx.swiperefreshlayout.widget.SwipeRefreshLayout

/**
 * Domshon for Android: a native shell around the server's web app (files, editor, sync status, admin
 * panel, device monitor, two-factor login). Asks for the server address on first start, keeps the
 * session in the WebView, supports uploads (system file picker) and downloads (DownloadManager).
 */
class MainActivity : AppCompatActivity() {
    private lateinit var web: WebView
    private lateinit var refresh: SwipeRefreshLayout
    private var chooser: ValueCallback<Array<Uri>>? = null
    private val prefs by lazy { getSharedPreferences("domshon", Context.MODE_PRIVATE) }

    private fun server(): String = (prefs.getString("server", null) ?: BuildConfig.DEFAULT_SERVER).trim().trimEnd('/')

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        web = WebView(this)
        refresh = SwipeRefreshLayout(this).apply { addView(web); setOnRefreshListener { web.reload() } }
        setContentView(refresh)

        web.settings.apply {
            javaScriptEnabled = true
            domStorageEnabled = true            // the web app keeps its session in localStorage
            allowFileAccess = false
            allowContentAccess = false
            mediaPlaybackRequiresUserGesture = true
            userAgentString = userAgentString + " DomshonAndroid/" + BuildConfig.VERSION_NAME
        }
        CookieManager.getInstance().setAcceptCookie(true)

        web.webViewClient = object : WebViewClient() {
            override fun shouldOverrideUrlLoading(v: WebView, r: WebResourceRequest): Boolean {
                val u = r.url.toString()
                if (u.startsWith(server())) return false       // stay inside the app
                startActivity(Intent(Intent.ACTION_VIEW, r.url)) // anything else opens in the browser
                return true
            }
            override fun onPageFinished(v: WebView, url: String) { refresh.isRefreshing = false }
            override fun onReceivedError(v: WebView, r: WebResourceRequest, e: WebResourceError) {
                if (r.isForMainFrame) showOffline(e.description?.toString() ?: "error")
            }
        }
        web.webChromeClient = object : WebChromeClient() {
            override fun onShowFileChooser(w: WebView, cb: ValueCallback<Array<Uri>>, p: FileChooserParams): Boolean {
                chooser?.onReceiveValue(null)
                chooser = cb
                return try {
                    @Suppress("DEPRECATION")
                    startActivityForResult(p.createIntent(), 42); true
                } catch (e: Exception) { chooser = null; false }
            }
        }
        web.setDownloadListener { url, ua, disp, mime, _ ->
            val name = URLUtil.guessFileName(url, disp, mime)
            val req = DownloadManager.Request(Uri.parse(url))
                .setMimeType(mime).addRequestHeader("User-Agent", ua)
                .addRequestHeader("Cookie", CookieManager.getInstance().getCookie(url) ?: "")
                .setNotificationVisibility(DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED)
                .setDestinationInExternalPublicDir(Environment.DIRECTORY_DOWNLOADS, name)
            (getSystemService(DOWNLOAD_SERVICE) as DownloadManager).enqueue(req)
            Toast.makeText(this, "Downloading $name", Toast.LENGTH_SHORT).show()
        }
        onBackPressedDispatcher.addCallback(this, object : OnBackPressedCallback(true) {
            override fun handleOnBackPressed() { if (web.canGoBack()) web.goBack() else finish() }
        })

        if (server().isEmpty()) askServer() else load()
    }

    private fun online(): Boolean {
        val cm = getSystemService(CONNECTIVITY_SERVICE) as ConnectivityManager
        return cm.activeNetworkInfo?.isConnected == true
    }

    private fun load() {
        if (!online()) { showOffline("no network"); return }
        web.loadUrl(server())
    }

    private fun showOffline(why: String) {
        refresh.isRefreshing = false
        AlertDialog.Builder(this).setTitle("Cannot reach the server")
            .setMessage("${server().ifEmpty { "(no server set)" }}\n\n$why")
            .setPositiveButton("Retry") { _, _ -> load() }
            .setNeutralButton("Change server") { _, _ -> askServer() }
            .setNegativeButton("Close", null).show()
    }

    private fun askServer() {
        val input = EditText(this).apply {
            hint = "https://files.example.com"
            inputType = InputType.TYPE_TEXT_VARIATION_URI
            setText(server())
        }
        AlertDialog.Builder(this).setTitle("Domshon server address").setView(input)
            .setCancelable(server().isNotEmpty())
            .setPositiveButton("Connect") { _, _ ->
                var u = input.text.toString().trim().trimEnd('/')
                if (u.isNotEmpty() && !u.startsWith("http")) u = "https://$u"
                prefs.edit().putString("server", u).apply()
                load()
            }.show()
    }

    @Deprecated("Deprecated in Java")
    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        if (requestCode == 42) {
            chooser?.onReceiveValue(WebChromeClient.FileChooserParams.parseResult(resultCode, data))
            chooser = null
        } else super.onActivityResult(requestCode, resultCode, data)
    }

    override fun onCreateOptionsMenu(menu: android.view.Menu): Boolean = false

    /** Long-press the title area is not available in a WebView shell; the web app's Settings has "Server". */
    override fun onKeyLongPress(keyCode: Int, event: android.view.KeyEvent?): Boolean {
        if (keyCode == android.view.KeyEvent.KEYCODE_MENU) { askServer(); return true }
        return super.onKeyLongPress(keyCode, event)
    }
}

// Win32 window capture helper for the screenshot tool.
//
// Why this is a separate .cs file rather than an inline Add-Type here-string:
// Get-Content/Set-Content round-trips and editors can silently alter the
// here-string terminator's leading whitespace, which makes PowerShell stop
// recognizing it and dump the C# as script text (a confusing wall of
// "missing using directive" errors). A real file has no such fragility.
//
// Compile with:
//   Add-Type -Path windowcapture.cs -ReferencedAssemblies System.Drawing

using System;
using System.Text;
using System.Drawing;
using System.Runtime.InteropServices;

public class WinCap
{
    public delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);

    [DllImport("user32.dll")]
    public static extern bool EnumWindows(EnumWindowsProc cb, IntPtr lParam);

    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    public static extern int GetWindowText(IntPtr hWnd, StringBuilder s, int n);

    [DllImport("user32.dll")]
    public static extern int GetWindowTextLength(IntPtr hWnd);

    [DllImport("user32.dll")]
    public static extern bool IsWindowVisible(IntPtr hWnd);

    [DllImport("user32.dll")]
    public static extern bool SetForegroundWindow(IntPtr hWnd);

    [DllImport("user32.dll")]
    public static extern bool ShowWindow(IntPtr hWnd, int cmd);

    [DllImport("user32.dll")]
    public static extern bool GetClientRect(IntPtr hWnd, out RECT r);

    [DllImport("user32.dll")]
    public static extern bool GetWindowRect(IntPtr hWnd, out RECT r);

    [DllImport("user32.dll")]
    public static extern bool ClientToScreen(IntPtr hWnd, ref POINT p);

    [DllImport("user32.dll")]
    public static extern bool PrintWindow(IntPtr hWnd, IntPtr hdc, uint flags);

    [StructLayout(LayoutKind.Sequential)]
    public struct RECT { public int Left, Top, Right, Bottom; }

    [StructLayout(LayoutKind.Sequential)]
    public struct POINT { public int X, Y; }

    /// <summary>Find the first visible window whose title contains needle.</summary>
    public static IntPtr Find(string needle)
    {
        IntPtr found = IntPtr.Zero;
        EnumWindows(delegate(IntPtr h, IntPtr l)
        {
            if (!IsWindowVisible(h)) return true;
            int n = GetWindowTextLength(h);
            if (n <= 0) return true;
            StringBuilder sb = new StringBuilder(n + 1);
            GetWindowText(h, sb, n + 1);
            if (sb.ToString().IndexOf(needle, StringComparison.OrdinalIgnoreCase) >= 0)
            {
                found = h;
                return false;
            }
            return true;
        }, IntPtr.Zero);
        return found;
    }

    /// <summary>List all visible window titles (diagnostics).</summary>
    public static string All()
    {
        StringBuilder outp = new StringBuilder();
        EnumWindows(delegate(IntPtr h, IntPtr l)
        {
            if (!IsWindowVisible(h)) return true;
            int n = GetWindowTextLength(h);
            if (n <= 0) return true;
            StringBuilder sb = new StringBuilder(n + 1);
            GetWindowText(h, sb, n + 1);
            outp.AppendLine(sb.ToString());
            return true;
        }, IntPtr.Zero);
        return outp.ToString();
    }

    public static string SizeOf(IntPtr h, bool useClient)
    {
        RECT r;
        bool ok = useClient ? GetClientRect(h, out r) : GetWindowRect(h, out r);
        if (!ok) return "0x0";
        return (r.Right - r.Left) + "x" + (r.Bottom - r.Top);
    }

    /// <summary>
    /// Ask the window to paint itself into a bitmap and save it.
    /// Works even when the window is covered by other windows.
    /// </summary>
    public static bool Grab(IntPtr h, string path, bool useClient)
    {
        RECT r;
        bool ok = useClient ? GetClientRect(h, out r) : GetWindowRect(h, out r);
        if (!ok) return false;
        int w = r.Right - r.Left;
        int hh = r.Bottom - r.Top;
        if (w <= 0 || hh <= 0) return false;

        using (Bitmap bmp = new Bitmap(w, hh))
        using (Graphics g = Graphics.FromImage(bmp))
        {
            IntPtr dc = g.GetHdc();
            bool painted = false;
            try
            {
                // 2 = PW_RENDERFULLCONTENT. Required for DirectComposition /
                // WebView2 surfaces; without it you get a blank bitmap.
                painted = PrintWindow(h, dc, 2);
                if (!painted) painted = PrintWindow(h, dc, 0);
            }
            finally
            {
                g.ReleaseHdc(dc);
            }
            if (!painted) return false;
            bmp.Save(path, System.Drawing.Imaging.ImageFormat.Png);
        }
        return true;
    }
}

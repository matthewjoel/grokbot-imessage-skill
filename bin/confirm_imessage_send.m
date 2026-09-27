#import <AppKit/AppKit.h>
#import <Foundation/Foundation.h>
#include <float.h>


static const NSTimeInterval kConfirmationTimeoutSeconds = 60.0;


@interface ConfirmationTimeout : NSObject
@property(nonatomic, assign) BOOL timedOut;
- (void)fire:(NSTimer *)timer;
@end


@implementation ConfirmationTimeout
- (void)fire:(NSTimer *)timer {
    (void)timer;
    self.timedOut = YES;
    [NSApp abortModal];
}
@end


static NSString *RequiredString(NSDictionary *payload, NSString *key) {
    id value = payload[key];
    return [value isKindOfClass:[NSString class]] ? value : nil;
}


// Referenced below so the token survives compilation. The Python helper
// refuses image sends unless this exact byte string is in the confirm binary.
static const char kImageConfirmCapability[] = "IMESSAGE_CONFIRM_IMAGE_V1";


static NSString *FormatByteCount(long long bytes) {
    if (bytes < 1024) {
        return [NSString stringWithFormat:@"%lld bytes", bytes];
    }
    if (bytes < (1024LL * 1024LL)) {
        return [NSString stringWithFormat:@"%lld bytes (%.1f KiB)",
                bytes, (double)bytes / 1024.0];
    }
    return [NSString stringWithFormat:@"%lld bytes (%.1f MiB)",
            bytes, (double)bytes / (1024.0 * 1024.0)];
}


// Returns nil when no image is being sent. On an invalid image object,
// writes an error and returns nil with *invalid set.
static NSDictionary *ImagePayload(NSDictionary *payload, BOOL *invalid) {
    id value = payload[@"image"];
    if (value == nil || value == (id)kCFNull) {
        return nil;
    }
    if (![value isKindOfClass:[NSDictionary class]]) {
        fprintf(stderr, "confirmation helper: image must be an object\n");
        *invalid = YES;
        return nil;
    }
    NSDictionary *image = value;
    NSString *filename = RequiredString(image, @"filename");
    NSString *mime = RequiredString(image, @"mime");
    NSString *path = RequiredString(image, @"path");
    id rawSize = image[@"size"];
    if (filename.length == 0 || mime.length == 0 || path.length == 0 ||
        ![rawSize isKindOfClass:[NSNumber class]] ||
        CFGetTypeID((__bridge CFTypeRef)rawSize) == CFBooleanGetTypeID()) {
        fprintf(stderr, "confirmation helper: image filename, type, size, and path are required\n");
        *invalid = YES;
        return nil;
    }
    if ([(NSNumber *)rawSize longLongValue] <= 0) {
        fprintf(stderr, "confirmation helper: image size is required\n");
        *invalid = YES;
        return nil;
    }
    // Volatile read keeps the capability token in the built binary even if
    // the compiler can see that the literal is non-empty.
    const volatile char *capability = kImageConfirmCapability;
    if (capability[0] == '\0') {
        *invalid = YES;
        return nil;
    }
    return image;
}


int main(void) {
    @autoreleasepool {
        NSData *input = [[NSFileHandle fileHandleWithStandardInput] readDataToEndOfFile];
        NSError *jsonError = nil;
        id decoded = [NSJSONSerialization JSONObjectWithData:input options:0 error:&jsonError];
        if (jsonError != nil || ![decoded isKindOfClass:[NSDictionary class]]) {
            fprintf(stderr, "confirmation helper: invalid JSON input\n");
            return 2;
        }

        NSDictionary *payload = decoded;
        NSString *clientName = RequiredString(payload, @"client_name");
        NSString *recipient = RequiredString(payload, @"to");
        NSString *resolvedName = RequiredString(payload, @"resolved_name");
        NSString *service = RequiredString(payload, @"service");
        NSString *message = RequiredString(payload, @"text");
        BOOL imageInvalid = NO;
        NSDictionary *image = ImagePayload(payload, &imageInvalid);
        if (imageInvalid) {
            return 2;
        }
        if (clientName.length == 0 || recipient.length == 0 ||
            service.length == 0 || message == nil ||
            (image == nil && message.length == 0)) {
            fprintf(stderr, "confirmation helper: required field missing\n");
            return 2;
        }

        [NSApplication sharedApplication];
        [NSApp setActivationPolicy:NSApplicationActivationPolicyAccessory];

        NSAlert *alert = [[NSAlert alloc] init];
        alert.alertStyle = NSAlertStyleWarning;
        // Product mode: the wrapper exports the host app's icon path
        // (IMESSAGE_HOST_ICON_PATH). This helper lives outside Contents/MacOS,
        // so without it the alert shows the generic process icon. Baked mode
        // leaves the variable unset and keeps the default.
        const char *iconPath = getenv("IMESSAGE_HOST_ICON_PATH");
        if (iconPath != NULL && iconPath[0] == '/') {
            NSImage *hostIcon = [[NSImage alloc]
                initWithContentsOfFile:[NSString stringWithUTF8String:iconPath]];
            if (hostIcon != nil && hostIcon.isValid) {
                alert.icon = hostIcon;
            }
        }
        alert.messageText = image == nil
            ? [NSString stringWithFormat:@"Confirm %@ %@ Send", clientName, service]
            : [NSString stringWithFormat:@"Confirm %@ %@ Image Send", clientName, service];
        NSString *displayRecipient = resolvedName.length > 0
            ? [NSString stringWithFormat:@"%@ (%@)", resolvedName, recipient]
            : recipient;
        NSMutableString *info = [NSMutableString stringWithFormat:
            @"Recipient: %@\nService: %@",
            displayRecipient,
            service];
        if (image != nil) {
            [info appendFormat:@"\nAttachment: %@\nType: %@\nSize: %@\nPath: %@",
                image[@"filename"],
                image[@"mime"],
                FormatByteCount([image[@"size"] longLongValue]),
                image[@"path"]];
        }
        [info appendFormat:@"\nMessage length: %lu characters",
            (unsigned long)message.length];
        alert.informativeText = info;

        NSScrollView *scrollView = [[NSScrollView alloc]
            initWithFrame:NSMakeRect(0, 0, 560, 280)];
        scrollView.borderType = NSBezelBorder;
        scrollView.hasVerticalScroller = YES;
        scrollView.hasHorizontalScroller = NO;

        NSTextView *textView = [[NSTextView alloc] initWithFrame:scrollView.bounds];
        textView.editable = NO;
        textView.selectable = YES;
        textView.richText = NO;
        textView.font = [NSFont systemFontOfSize:[NSFont systemFontSize]];
        textView.string = (image != nil && message.length == 0) ? @"(no caption)" : message;
        textView.verticallyResizable = YES;
        textView.horizontallyResizable = NO;
        textView.autoresizingMask = NSViewWidthSizable;
        textView.textContainer.containerSize = NSMakeSize(
            scrollView.contentSize.width, FLT_MAX);
        textView.textContainer.widthTracksTextView = YES;
        scrollView.documentView = textView;
        alert.accessoryView = scrollView;

        NSButton *cancelButton = [alert addButtonWithTitle:@"Cancel"];
        NSButton *sendButton = [alert addButtonWithTitle:@"Send"];
        cancelButton.keyEquivalent = @"\r";
        sendButton.keyEquivalent = @"";

        ConfirmationTimeout *timeout = [[ConfirmationTimeout alloc] init];
        NSTimer *timer = [NSTimer timerWithTimeInterval:kConfirmationTimeoutSeconds
                                                 target:timeout
                                               selector:@selector(fire:)
                                               userInfo:nil
                                                repeats:NO];
        [[NSRunLoop currentRunLoop] addTimer:timer forMode:NSModalPanelRunLoopMode];

        [NSApp activateIgnoringOtherApps:YES];
        NSModalResponse response = [alert runModal];
        [timer invalidate];

        if (timeout.timedOut) {
            return 3;
        }
        return response == NSAlertSecondButtonReturn ? 0 : 1;
    }
}
